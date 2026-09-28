"""
hydro/rainfall_runoff.py

功能：
  1) 提供 abcd_step / gr2m_step / coefficient_step 供 engine/simulator.py 回退调用
  2) 用 ABCD+Snow+f_pet 计算整段时间序列的：
     - 总径流 runoff
     - 地下水补给 gw_recharge
     - 基流 baseflow
  3) 输出 3 个csv（万m³/月），时间轴来自 climate/precip.csv 的 date 列

用法：
  python hydro/rainfall_runoff.py
"""

import os
import sys
import time as _time
import numpy as np
import pandas as pd

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

DATA_DIR = "data"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from output_paths import HYDRO_CACHE, resolve_output_file

OUTPUT_DIR = str(HYDRO_CACHE)

try:
    from numba import njit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

def read_auto(path):
    for enc in ["utf-8-sig", "utf-8", "gbk", "gb18030", "ansi"]:
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception:
            continue
    raise ValueError(f"无法读取 {path}")

# =========================================================
# simulator 回退用：单步函数（必须存在，供 import）
# =========================================================

def abcd_step(P, PET, a, b, c, d, S_soil, S_gw, area):
    """ABCD 单步（不含融雪、无f_pet；仅作回退）"""
    b_ = max(1.0, b)
    a_ = max(0.01, min(1.0, a))

    W = P + S_soil
    t1 = (W + b_) / (2.0 * a_)
    disc = max(0.0, t1 * t1 - W * b_ / a_)
    Y = max(0.0, min(t1 - disc**0.5, W))

    ET = max(0.0, min(Y * (1.0 - np.exp(-PET / b_)), Y))
    S_soil_new = max(0.0, Y - ET)

    excess = max(0.0, W - Y)
    gw = c * excess
    direct = excess - gw

    S_gw_new = (S_gw + gw) / (1.0 + d)
    bf = d * S_gw_new

    total = (direct + bf) * area * 0.1
    surface = direct * area * 0.1
    baseflow_val = bf * area * 0.1
    gw_rech = gw * area * 0.1

    return {
        "total_runoff": max(0.0, total),
        "surface": max(0.0, surface),
        "baseflow": max(0.0, baseflow_val),
        "gw_recharge": max(0.0, gw_rech),
        "S_soil": S_soil_new,
        "S_gw": S_gw_new,
    }

def gr2m_step(P, PET, x1, x2, S, R, area):
    """GR2M 单步（回退用）"""
    x1 = max(1.0, x1)
    phi = np.tanh(P / x1)
    psi = np.tanh(PET / x1)

    S1 = (S + x1 * phi) / (1 + phi * S / x1)
    P1 = P + S - S1
    S2 = S1 * (1 - psi) / (1 + psi * (1 - S1 / x1))
    S_new = S2

    P2 = P1 + S1 - S2
    R_new = max(0.0, R + P2)
    Q = R_new * (1 - 1 / (1 + (R_new / max(1.0, x1 * x2)) ** 2) ** 0.5)
    R_new = max(0.0, R_new - Q)

    total = Q * area * 0.1
    return {
        "total_runoff": max(0.0, total),
        "surface": max(0.0, total * 0.7),
        "baseflow": max(0.0, total * 0.3),
        "gw_recharge": max(0.0, total * 0.2),
        "S": S_new,
        "R": R_new,
    }

def coefficient_step(P, PET, et_reduction, runoff_coeff, baseflow_index, area):
    """系数法单步（回退用）"""
    AET = PET * (1 - et_reduction)
    net_P = max(0.0, P - AET)
    total = net_P * runoff_coeff * area * 0.1
    surface = total * (1 - baseflow_index)
    bf = total * baseflow_index
    return {
        "total_runoff": max(0.0, total),
        "surface": max(0.0, surface),
        "baseflow": max(0.0, bf),
        "gw_recharge": max(0.0, bf * 0.5),
    }

# =========================================================
# 全序列：ABCD+Snow+f_pet，输出 runoff/gw/baseflow
# =========================================================

if HAS_NUMBA:
    @njit(cache=True)
    def _abcd_run_full(P, PET, T, area, a, b, c, d, f_pet, Ts, Tm, n):
        total_ro = np.zeros(n)
        gw_rech = np.zeros(n)
        baseflow = np.zeros(n)

        S, G, Snow = 100.0, 50.0, 0.0
        b_ = b if b > 1.0 else 1.0
        a_ = a
        if a_ < 0.01:
            a_ = 0.01
        if a_ > 1.0:
            a_ = 1.0

        fp = f_pet
        if fp < 0.1:
            fp = 0.1
        if fp > 5.0:
            fp = 5.0

        for t in range(n):
            p = P[t]
            if p < 0:
                p = 0.0
            pet = PET[t] * fp
            if pet < 0:
                pet = 0.0
            temp = T[t]

            # snow
            if temp < Ts:
                p_rain = 0.0
                Snow += p
            else:
                p_rain = p
                if Snow > 0:
                    melt = Tm * (temp - Ts)
                    if melt < 0:
                        melt = 0.0
                    if melt > Snow:
                        melt = Snow
                    Snow -= melt
                    p_rain += melt

            W = p_rain + S
            t1 = (W + b_) / (2.0 * a_)
            disc = t1 * t1 - W * b_ / a_
            if disc < 0:
                disc = 0.0
            Y = t1 - np.sqrt(disc)
            if Y < 0:
                Y = 0.0
            if Y > W:
                Y = W

            ET = Y * (1.0 - np.exp(-pet / b_))
            if ET < 0:
                ET = 0.0
            if ET > Y:
                ET = Y

            S = Y - ET
            if S < 0:
                S = 0.0

            excess = W - Y
            if excess < 0:
                excess = 0.0

            gw = c * excess
            direct = excess - gw

            G = (G + gw) / (1.0 + d)
            bf = d * G

            total_ro[t] = (direct + bf) * area * 0.1
            gw_rech[t] = gw * area * 0.1
            baseflow[t] = bf * area * 0.1

        return total_ro, gw_rech, baseflow
else:
    def _abcd_run_full(P, PET, T, area, a, b, c, d, f_pet, Ts, Tm, n):
        total_ro = np.zeros(n)
        gw_rech = np.zeros(n)
        baseflow = np.zeros(n)

        S, G, Snow = 100.0, 50.0, 0.0
        b_ = max(1.0, b)
        a_ = max(0.01, min(1.0, a))
        fp = max(0.1, min(5.0, f_pet))

        for t in range(n):
            p = max(0.0, P[t])
            pet = max(0.0, PET[t] * fp)
            temp = T[t]

            if temp < Ts:
                p_rain = 0.0
                Snow += p
            else:
                p_rain = p
                if Snow > 0:
                    melt = min(Snow, max(0.0, Tm * (temp - Ts)))
                    Snow -= melt
                    p_rain += melt

            W = p_rain + S
            t1 = (W + b_) / (2.0 * a_)
            disc = max(0.0, t1 * t1 - W * b_ / a_)
            Y = max(0.0, min(t1 - np.sqrt(disc), W))

            ET = max(0.0, min(Y * (1.0 - np.exp(-pet / b_)), Y))
            S = max(0.0, Y - ET)

            excess = max(0.0, W - Y)
            gw = c * excess
            direct = excess - gw

            G = (G + gw) / (1.0 + d)
            bf = d * G

            total_ro[t] = (direct + bf) * area * 0.1
            gw_rech[t] = gw * area * 0.1
            baseflow[t] = bf * area * 0.1

        return total_ro, gw_rech, baseflow

def _get_ts(uid, df, nt):
    if uid in df.columns:
        v = pd.to_numeric(df[uid], errors="coerce").values.astype(np.float64)
        v = np.nan_to_num(v)
        if len(v) < nt:
            v = np.pad(v, (0, nt - len(v)), mode="edge")
        return v[:nt]
    return np.zeros(nt, dtype=np.float64)

def main():
    t0 = _time.time()
    print("=" * 70)
    print("  ABCD+Snow+f_pet 产流 + 地下水补给 + 基流（时间轴来自气象date）")
    print("=" * 70)
    print(f"  numba: {'✓' if HAS_NUMBA else '✗'}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1) 读气象（用 precip 的 date 作为全局时间轴）
    P_df = read_auto(os.path.join(DATA_DIR, "climate", "precip.csv"))
    PET_df = read_auto(os.path.join(DATA_DIR, "climate", "pet.csv"))
    T_df = read_auto(os.path.join(DATA_DIR, "climate", "temp_mean.csv"))

    if "date" not in P_df.columns:
        raise ValueError("data/climate/precip.csv 缺少 date 列")
    sim_dates = pd.to_datetime(P_df["date"])
    nt = len(sim_dates)
    print(f"  模拟期: {sim_dates.iloc[0].strftime('%Y-%m')} ~ {sim_dates.iloc[-1].strftime('%Y-%m')}  (nt={nt})")

    date_strs = sim_dates.dt.strftime("%Y-%m").tolist()

    # 2) 单元/面积
    units_df = read_auto(os.path.join(DATA_DIR, "units.csv"))
    uc = [c for c in units_df.columns if c.strip().upper() == "UID"][0]
    ac = [c for c in units_df.columns if c.strip().upper() == "AREA"][0]
    mc = [c for c in units_df.columns if c.strip() == "干流"][0]

    uid_area = {}
    sub_uids = []
    for _, r in units_df.iterrows():
        uid = str(r[uc]).strip()
        uid_area[uid] = float(r[ac])
        if int(r[mc]) == 0:
            sub_uids.append(uid)

    # 3) 参数（来自 output/calibrated_params.csv）
    params_path = str(resolve_output_file("calibrated_params.csv"))
    uid_params = {}
    if os.path.exists(params_path):
        params_df = read_auto(params_path)
        for _, r in params_df.iterrows():
            uid = str(r["unit_id"]).strip()
            uid_params[uid] = {
                "a": float(r.get("param_a", 0.93)),
                "b": float(r.get("param_b", 200)),
                "c": float(r.get("param_c", 0.25)),
                "d": float(r.get("param_d", 0.15)),
                "f_pet": float(r.get("param_f_pet", 1.5)),
                "Ts": float(r.get("param_Ts", 1.0)),
                "Tm": float(r.get("param_Tm", 4.0)),
            }

    default_p = {"a": 0.93, "b": 200, "c": 0.25, "d": 0.15, "f_pet": 1.5, "Ts": 1.0, "Tm": 4.0}
    print(f"  非干流单元: {len(sub_uids)}  |  已有参数: {len(uid_params)}")

    # numba预热
    if HAS_NUMBA:
        _d = np.zeros(10, dtype=np.float64)
        _ = _abcd_run_full(_d, _d, _d, 1000.0, 0.93, 200, 0.25, 0.15, 1.5, 1.0, 4.0, 10)

    out_ro = {"date": date_strs}
    out_gw = {"date": date_strs}
    out_bf = {"date": date_strs}

    for i, uid in enumerate(sub_uids):
        if i % 50 == 0:
            print(f"\r  进度: {i}/{len(sub_uids)}", end="", flush=True)

        p = uid_params.get(uid, default_p)
        P = _get_ts(uid, P_df, nt)
        PET = _get_ts(uid, PET_df, nt)
        T = _get_ts(uid, T_df, nt)
        area = uid_area.get(uid, 1000.0)

        ro, gw, bf = _abcd_run_full(P, PET, T, area,
                                    p["a"], p["b"], p["c"], p["d"],
                                    p["f_pet"], p["Ts"], p["Tm"], nt)

        out_ro[uid] = np.round(ro, 2)
        out_gw[uid] = np.round(gw, 2)
        out_bf[uid] = np.round(bf, 2)

    print(f"\r  进度: {len(sub_uids)}/{len(sub_uids)} 完成")

    pd.DataFrame(out_ro).to_csv(os.path.join(OUTPUT_DIR, "runoff_monthly.csv"),
                                index=False, encoding="utf-8-sig")
    pd.DataFrame(out_gw).to_csv(os.path.join(OUTPUT_DIR, "gw_recharge_monthly.csv"),
                                index=False, encoding="utf-8-sig")
    pd.DataFrame(out_bf).to_csv(os.path.join(OUTPUT_DIR, "baseflow_monthly.csv"),
                                index=False, encoding="utf-8-sig")

    print("  ✅ output/runoff_monthly.csv")
    print("  ✅ output/gw_recharge_monthly.csv")
    print("  ✅ output/baseflow_monthly.csv")
    print(f"  耗时: {(_time.time()-t0)/60:.1f} 分钟")

if __name__ == "__main__":
    main()
