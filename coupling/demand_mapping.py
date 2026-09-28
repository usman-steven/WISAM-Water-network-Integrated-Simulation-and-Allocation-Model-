from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from typing import Dict, List


@dataclass
class UnitCityLink:
    unit_id: str
    city_code: str
    city_name: str
    unit_name: str = ""
    zone_id: str = ""
    zone_name: str = ""
    basin_l2: str = ""
    basin: str = ""
    province: str = ""
    area: float = 0.0
    weight: float = 0.0


class DemandMappingRegistry:
    def __init__(self):
        self.links: List[UnitCityLink] = []
        self.by_city: Dict[str, List[UnitCityLink]] = defaultdict(list)
        self.by_unit: Dict[str, List[UnitCityLink]] = defaultdict(list)

    def add(self, link: UnitCityLink):
        self.links.append(link)
        self.by_city[link.city_code].append(link)
        self.by_unit[link.unit_id].append(link)

    def get_city_links(self, city_code: str) -> List[UnitCityLink]:
        return list(self.by_city.get(str(city_code), []))

    def get_unit_links(self, unit_id: str) -> List[UnitCityLink]:
        return list(self.by_unit.get(str(unit_id), []))

    def get_city_units(self, city_code: str) -> List[str]:
        return [link.unit_id for link in self.get_city_links(city_code)]

    def get_city_weight(self, city_code: str, unit_id: str) -> float:
        for link in self.by_city.get(str(city_code), []):
            if link.unit_id == str(unit_id):
                return float(link.weight)
        return 0.0

    def resolve_demand_ref(self, demand_ref: str, ratio: float, existing_node_ids) -> Dict[str, float]:
        raw_ref = str(demand_ref).strip()
        if raw_ref in existing_node_ids:
            return {raw_ref: float(ratio)}

        raw = raw_ref[3:] if raw_ref.startswith('DM_') else raw_ref

        if raw in self.by_city:
            expanded = {}
            for link in self.by_city.get(raw, []):
                node_id = f'DM_{link.unit_id}'
                if node_id in existing_node_ids:
                    expanded[node_id] = expanded.get(node_id, 0.0) + float(ratio) * float(link.weight)
            if expanded:
                return expanded

        if raw in self.by_unit and self.by_unit[raw]:
            node_id = f'DM_{raw}'
            if node_id in existing_node_ids:
                return {node_id: float(ratio)}
        return {}

    @classmethod
    def from_units_df(cls, units_df) -> "DemandMappingRegistry":
        reg = cls()
        if units_df is None or len(units_df) == 0:
            return reg

        city_totals = defaultdict(float)
        for _, row in units_df.iterrows():
            if int(row['is_mainstream']) == 0:
                city_totals[str(row['cid'])] += float(row['area'])

        for _, row in units_df.iterrows():
            if int(row['is_mainstream']) != 0:
                continue
            city_code = str(row['cid'])
            total_area = max(1e-6, city_totals[city_code])
            reg.add(UnitCityLink(
                unit_id=str(row['uid']),
                city_code=city_code,
                city_name=str(row.get('cname', '')),
                unit_name=str(row.get('uname', '')),
                zone_id=str(row.get('wid', '')),
                zone_name=str(row.get('wname', '')),
                basin_l2=str(row.get('basin_l2', '')),
                basin=str(row.get('basin', '')),
                province=str(row.get('province', '')),
                area=float(row['area']),
                weight=float(row['area']) / total_area,
            ))
        return reg
