"""Lake regulation module.

The simulator calls :meth:`init_lake` once before a run and :meth:`step` once
per monthly time step. Volumes use the project-wide unit of 10^4 m3.
"""

from __future__ import annotations

import numpy as np

from core.nodes import LakeNode


class LakeModule:
    """Monthly lake storage and outflow regulation."""

    # km2 * mm -> 10^4 m3: 1 km2 * 1 mm = 0.1 * 10^4 m3.
    EVAP_VOLUME_FACTOR = 0.1

    def __init__(self):
        pass

    @staticmethod
    def _as_nonnegative(value: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not np.isfinite(value):
            return 0.0
        return max(0.0, value)

    @staticmethod
    def _month_index(month: int) -> int:
        return min(11, max(0, int(month) - 1))

    @staticmethod
    def _storage_bounds(lake: LakeNode) -> tuple[float, float, float, float]:
        dead = max(0.0, float(lake.dead_storage or 0.0))
        eco_min = max(dead, float(lake.eco_min_storage or 0.0))
        normal = max(eco_min, float(lake.normal_storage or 0.0))
        max_storage = max(normal, float(lake.max_storage or 0.0))
        return dead, eco_min, normal, max_storage

    def init_lake(self, lake: LakeNode, ratio: float = None,
                  n_steps: int = 732):
        """Initialize lake storage and result series."""
        dead, _, normal, max_storage = self._storage_bounds(lake)
        usable = max(0.0, normal - dead)

        if ratio is not None:
            ratio = min(1.0, max(0.0, float(ratio)))
            lake.current_storage = dead + usable * ratio
        elif self._as_nonnegative(lake.current_storage) <= 0.0:
            lake.current_storage = dead + usable * 0.5

        lake.current_storage = min(
            max_storage,
            max(0.0, float(lake.current_storage or 0.0)),
        )

        # Keep disconnected lakes at their initial storage for audit stability.
        lake.storage_ts = np.full(n_steps, lake.current_storage)
        lake.inflow_ts = np.zeros(n_steps)
        lake.outflow_ts = np.zeros(n_steps)
        lake.evap_loss_ts = np.zeros(n_steps)
        lake.release_ts = np.zeros(n_steps)
        lake.spill_ts = np.zeros(n_steps)

    def _evap_loss(self, lake: LakeNode, month: int) -> float:
        storage = self._as_nonnegative(lake.current_storage)
        if storage <= 0.0:
            return 0.0
        area = self._as_nonnegative(lake.area_coeff) * (storage ** 0.667)
        evap_mm = self._as_nonnegative(lake.monthly_evap[self._month_index(month)])
        return area * evap_mm * self.EVAP_VOLUME_FACTOR

    def step(self, lake: LakeNode, t: int, month: int,
             inflow: float, demand: float = 0,
             transfer_withdraw: float = 0) -> dict:
        """Advance one monthly lake-regulation step.

        The controlled release is capped by the outlet capacity and by volume
        above the ecological minimum storage. Any remaining volume above maximum
        storage is counted as spill.
        """
        inflow = self._as_nonnegative(inflow)
        demand = self._as_nonnegative(demand)
        transfer_withdraw = self._as_nonnegative(transfer_withdraw)

        _, eco_min, normal, max_storage = self._storage_bounds(lake)
        outlet_capacity = self._as_nonnegative(lake.outflow_capacity)
        evap = self._evap_loss(lake, month)

        pool = (
            self._as_nonnegative(lake.current_storage) +
            inflow -
            evap -
            transfer_withdraw
        )
        pool = max(0.0, pool)

        available_above_eco = max(0.0, pool - eco_min)
        demand_release = min(demand, available_above_eco)
        drawdown_release = max(0.0, pool - normal)

        if pool > normal:
            release_target = max(demand_release, drawdown_release)
        elif pool > eco_min:
            release_target = demand_release
        else:
            release_target = 0.0

        release = min(release_target, available_above_eco, outlet_capacity)
        new_storage = pool - release

        spill = 0.0
        if new_storage > max_storage:
            spill = new_storage - max_storage
            new_storage = max_storage

        lake.current_storage = max(0.0, new_storage)
        total_outflow = release + spill

        if lake.storage_ts is not None and t < len(lake.storage_ts):
            lake.storage_ts[t] = lake.current_storage
        if lake.inflow_ts is not None and t < len(lake.inflow_ts):
            lake.inflow_ts[t] = inflow
        if lake.outflow_ts is not None and t < len(lake.outflow_ts):
            lake.outflow_ts[t] = total_outflow
        if getattr(lake, 'evap_loss_ts', None) is not None and t < len(lake.evap_loss_ts):
            lake.evap_loss_ts[t] = evap
        if getattr(lake, 'release_ts', None) is not None and t < len(lake.release_ts):
            lake.release_ts[t] = release
        if getattr(lake, 'spill_ts', None) is not None and t < len(lake.spill_ts):
            lake.spill_ts[t] = spill

        return {
            'outflow': total_outflow,
            'release': release,
            'spill': spill,
            'storage': lake.current_storage,
            'evap': evap,
            'transfer_withdraw': transfer_withdraw,
        }
