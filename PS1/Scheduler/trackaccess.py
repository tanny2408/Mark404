import json, math, os, datetime as dt
from collections import defaultdict
import pandas as pd
from ortools.sat.python import cp_model
DATA_DIR = "."
OUT_DIR = "out"
SCENARIOS = ["A", "B", "C"]
TIME_LIMIT = 600
MODE = "solve"
VALIDATE_DIR = "out/A"
CONFIG = dict(
    platform_mode="inclusive",
    strict_cosharing=False,
    default_supply=0,
    week_base=1,
    extra_weeks=8,
    max_extra_weeks=64,
    max_per_possession=4,
    workers=8,
    eclo_yield=3, std_yield=2,
    w_excess=7,
    w_eclo=5,
    tier_weight={1: 100, 2: 10, 3: 1},
    act_nudge={1: 3, 2: 2, 3: 0},
    c_excess_allowance=1,
    eclo_window_weeks=2,
    tie_access=1, tie_finish=1)
def find_data_dir(path):
    if os.path.isfile(os.path.join(path, "08_ACTIVITY_DETAILS.csv")): return path
    for root, _, files in os.walk(path):
        if "08_ACTIVITY_DETAILS.csv" in files: return root
    raise SystemExit(f"could not find 08_ACTIVITY_DETAILS.csv under '{path}' - set DATA_DIR to the instance folder")
def day_weight(inst, a):
    d = inst["act"][a]
    return CONFIG["tier_weight"][inst["tier"][d["contract"]]] * (10 + CONFIG["act_nudge"][d["prio"]])
def to_bool(v):
    return str(v).strip().lower() in ("1", "y", "yes", "true", "t")
def norm_nature(s):
    return "".join(str(s).split()).lower().replace("-", "").replace("(", "").replace(")", "")
def supply_at(inst, l, w):
    per = inst["cap_week"].get(l)
    if per: return int(per.get(w, inst["cap"].get(l, CONFIG["default_supply"])))
    return int(inst["cap"].get(l, CONFIG["default_supply"]))
def week_of(inst, d):
    return (pd.Timestamp(d).date() - inst["h0"]).days // 7 + CONFIG["week_base"]
def week_end(inst, w):
    return inst["h0"] + dt.timedelta(days=7 * (w - CONFIG["week_base"]) + 6)
def span_locations(inst, line, bound, i, j):
    seclist = inst["seclist"][line]
    secs = seclist[max(i, 0):j + 1]
    locs = [f"{s}:{bound}" for s in secs]
    for k in range(max(i, 0), j):
        locs.append(f"PLAT:{line}:{inst['secrow'].loc[seclist[k], 'to_station_id']}:{bound}")
    if CONFIG["platform_mode"] == "inclusive" and secs:
        locs.append(f"PLAT:{line}:{inst['secrow'].loc[secs[0], 'from_station_id']}:{bound}")
        locs.append(f"PLAT:{line}:{inst['secrow'].loc[secs[-1], 'to_station_id']}:{bound}")
    return list(dict.fromkeys(locs))
def bound_of(loc):
    p = loc.split(":")
    if len(p) < 4 or not p[3]: raise SystemExit(f"location '{loc}' has no bound suffix (expected ...:EB or ...:WB)")
    return p[3]
def station_set(inst, loc):
    p = loc.split(":")
    if p[0] == "PLAT": return {p[2]}
    key = f"SEC:{p[1]}:{p[2]}"
    if key in inst["secrow"].index: return {inst["secrow"].loc[key, "from_station_id"], inst["secrow"].loc[key, "to_station_id"]}
    return set()
def endpoint_options(inst, loc):
    p = loc.split(":")
    kind, line, mid, bound = p[0], p[1], p[2], (p[3] if len(p) > 3 else None)
    if line not in inst["seclist"]: raise SystemExit(f"unknown line in location '{loc}'")
    if kind == "SEC":
        key = f"SEC:{line}:{mid}"
        if key not in inst["secpos"][line]: raise SystemExit(f"unknown sector in location '{loc}'")
        return line, bound, [inst["secpos"][line][key]], None
    if kind == "PLAT":
        idx = [i for i, s in enumerate(inst["seclist"][line])
               if mid in (inst["secrow"].loc[s, "from_station_id"], inst["secrow"].loc[s, "to_station_id"])]
        if not idx: raise SystemExit(f"station '{mid}' is on no sector of line {line} (location '{loc}')")
        return line, bound, idx, f"PLAT:{line}:{mid}:{bound}"
    raise SystemExit(f"unknown location kind in '{loc}'")
def expand(inst, start_loc, end_loc, nature):
    l1, b1, opts1, plat1 = endpoint_options(inst, start_loc)
    l2, b2, opts2, plat2 = endpoint_options(inst, end_loc)
    if l1 != l2 or b1 != b2: raise SystemExit(f"activity spans two lines or bounds: {start_loc} -> {end_loc}")
    line, bound = l1, b1
    i, j = min(((min(x, y), max(x, y)) for x in opts1 for y in opts2), key=lambda q: q[1] - q[0])
    footprint = list(dict.fromkeys(span_locations(inst, line, bound, i, j) + [p for p in (plat1, plat2) if p]))
    if norm_nature(nature) not in inst["buf"]:
        raise SystemExit(f"nature_of_activity '{nature}' is not listed in 05_BUFFER_LOCATION.csv")
    nb, mirror = inst["buf"][norm_nature(nature)]
    closure = set(footprint)
    if nb:
        closure |= set(span_locations(inst, line, bound, max(0, i - nb), min(len(inst["seclist"][line]) - 1, j + nb)))
    if mirror:
        ob = "WB" if bound == "EB" else "EB"
        closure |= {l[:-2] + ob for l in list(closure)}
        hubs = inst["interchanges"]
        if any(station_set(inst, l) & hubs for l in closure):
            for other in inst["lines"].line_code:
                if other == line: continue
                for s in inst["seclist"][other]:
                    if {inst["secrow"].loc[s, "from_station_id"], inst["secrow"].loc[s, "to_station_id"]} <= hubs:
                        closure |= {f"{s}:{b}" for b in ("EB", "WB")}
                closure |= {f"PLAT:{other}:{h}:{b}" for h in hubs for b in ("EB", "WB") if f"PLAT:{other}:{h}:EB" in inst["cap"]}
    return footprint, sorted(closure)
def check_cycles(act):
    seen, stack = set(), set()
    def walk(x):
        if x in stack: raise SystemExit(f"predecessor cycle at {x}")
        if x in seen: return
        stack.add(x); seen.add(x)
        if act[x]["pred"]: walk(act[x]["pred"])
        stack.discard(x)
    for a in act: walk(a)
def load_instance(path): # THIS FUCNTION READ ALL CSV FILE AND EXTRACT DATA INTO LIBRARIES
    path = find_data_dir(path)
    rd = lambda f: pd.read_csv(os.path.join(path, f))
    params_df = rd("06_PARAMETERS.csv")
    inst = dict(path=path, lines=rd("01_LINES.csv"), stations=rd("02_STATIONS.csv"), sectors=rd("03_SECTORS.csv"),
    supply=rd("04_LOCATION_SUPPLY.csv"), buffers=rd("05_BUFFER_LOCATION.csv"), params=dict(zip(params_df.key, params_df.value)),
    contracts=rd("07_PROJECT_DETAILS.csv").set_index("contract_number"), acts=rd("08_ACTIVITY_DETAILS.csv").set_index("activity_id"))
    inst["h0"] = pd.Timestamp(inst["params"]["horizon_start"]).date() # HO IS THE HORIZON START DATE NOW
    inst["horizon_weeks"] = int(inst["params"]["horizon_weeks"]) # THIS IS THE NUMBER OF HORIZON WEEKS
    inst["W"] = inst["horizon_weeks"] + CONFIG["extra_weeks"] # AVIALABLE HORIZON + ADDED HORIZON
    sup = inst["supply"] 
    wcol = next((c for c in sup.columns if c.lower() in ("week", "week_no", "week_number", "week_index")), None)
    inst["cap"] = sup.groupby("location_id").supply_capacity.max().to_dict()
    inst["cap_week"] = {} if wcol is None else {l: dict(zip(g[wcol].astype(int), g.supply_capacity.astype(int))) for l, g in sup.groupby("location_id")}
    inst["supply_week_column"] = wcol
    inst["buf"] = {norm_nature(r.nature_of_works): (int(float(r.up_to_buffer_sectors)), to_bool(r.opposite_bound_required)) for r in inst["buffers"].itertuples()}
    inst["seclist"] = {l: list(inst["sectors"][inst["sectors"].line_code == l].sort_values("seq").sector_id) for l in inst["lines"].line_code}
    inst["secpos"] = {l: {s: i for i, s in enumerate(v)} for l, v in inst["seclist"].items()}
    inst["secrow"] = inst["sectors"].set_index("sector_id")
    st = inst["stations"]
    flagged = set(st[st.is_interchange.map(to_bool)].station_id) if "is_interchange" in st.columns else set()
    shared = set(st.groupby("station_id").line_code.nunique().pipe(lambda x: x[x > 1].index))
    inst["interchanges"] = flagged | shared
    c = inst["contracts"]
    inst["nature"], inst["access_type"] = c.nature_of_activity.to_dict(), c.access_type.to_dict()
    inst["wf"], inst["maxweek_access"] = c.number_of_workfronts.to_dict(), c.number_of_maximum_access_per_week.to_dict()
    inst["tier"] = c.contract_priority.to_dict()
    inst["planned_week"] = {k: week_of(inst, v) for k, v in c.planned_completion_date.items()}
    inst["planned_date"] = {k: pd.Timestamp(v).date() for k, v in c.planned_completion_date.items()}
    inst["A"] = list(inst["acts"].index)
    inst["act"] = {}
    for aid, r in inst["acts"].iterrows():
        cn = r.contract_number
        fp, cl = expand(inst, r.start_location_id, r.end_location_id, inst["nature"][cn])
        acc_col = next((c for c in inst["acts"].columns if c.lower() in ("access_type", "possession_type", "access")), None)
        need = float(r.total_accesses)
        inst["act"][aid] = dict(id=aid, contract=cn, type=r.activity_type, need=need, prio=int(r.activity_priority),
                                start_week=week_of(inst, r.planned_start_date), pred=None if pd.isna(r.predecessor_activity_id) else r.predecessor_activity_id,
                                nature=inst["nature"][cn], access=(getattr(r, acc_col) if acc_col else inst["access_type"][cn]), footprint=fp, closure=cl,
                                line=r.start_location_id.split(":")[1], bound=bound_of(r.start_location_id),
                                lines_affected=sorted({l.split(":")[1] for l in cl}))
    for aid, a in inst["act"].items():
        if a["pred"] and a["pred"] not in inst["act"]: raise SystemExit(f"{aid}: unknown predecessor {a['pred']}")
    check_cycles(inst["act"])
    booked = sorted({l for a in inst["act"].values() for l in a["footprint"] if l not in inst["cap"]})
    if booked: raise SystemExit(f"{len(booked)} booked location(s) have no row in 04_LOCATION_SUPPLY.csv: {booked[:5]}")
    closed_only = sorted({l for a in inst["act"].values() for l in a["closure"] if l not in inst["cap"]})
    if closed_only: print(f"warning: {len(closed_only)} location(s) are closed by a buffer but absent from 04_LOCATION_SUPPLY.csv: {closed_only[:5]}")
    need = max((inst["act"][a]["start_week"] + math.ceil(inst["act"][a]["need"]) + chain_depth(inst, a) for a in inst["A"]), default=0)
    inst["W"] = int(max(inst["W"], need + CONFIG["extra_weeks"]))
    return inst
def chain_depth(inst, a, seen=None): # a is the activity id here, ids already walked in this path
    seen = seen or set()
    p = inst["act"][a]["pred"] # check for predessors
    if not p or p in seen: return 0 # find the predissor is in the seen list
    seen.add(p)
    return math.ceil(inst["act"][p]["need"]) + chain_depth(inst, p, seen)
def has_buffer(inst, a): # check wheter selected activity has a buffer, for Live 2 adjecent buffers, 1 for buffer for non live, PC no buffer other can work there but
    return inst["buf"].get(norm_nature(inst["act"][a]["nature"]), (0, 0))[0] > 0 or inst["act"][a]["access"] == "PM"
def co_shareable(inst, a, b): # this check for all selected two activities can share the same location
    ta, tb = inst["act"][a]["access"], inst["act"][b]["access"]
    return ta != "PM" and tb != "PM" and not (ta == "PC" and tb == "PC")
def conflict_pairs(inst):
    out, A = [], inst["A"] # here a is activity ids
    for x in range(len(A)):
        for y in range(x + 1, len(A)): # this consider all pairs of activiites
            a, b = A[x], A[y]
            if not (has_buffer(inst, a) or has_buffer(inst, b)): continue # for activities need buffer need to skip
            fa, fb = set(inst["act"][a]["footprint"]), set(inst["act"][b]["footprint"])
            ca, cb = set(inst["act"][a]["closure"]), set(inst["act"][b]["closure"])
            overlap = bool(fa & fb)
            if overlap and co_shareable(inst, a, b):
                if not CONFIG["strict_cosharing"] or not ((ca & cb) - (fa & fb)): continue
                out.append((a, b)); continue
            if ca & cb: out.append((a, b)) # this will create a list of non confilcting pairs
    return out
def pack_groups(inst, acts_here):
    if any(has_buffer(inst, a) for a in acts_here): return [list(acts_here)] # in any activity selected as a better return just the list
    pm = [a for a in acts_here if inst["act"][a]["access"] == "PM"] # isolate each type of activity
    pc = [a for a in acts_here if inst["act"][a]["access"] == "PC"]
    co = [a for a in acts_here if inst["act"][a]["access"] == "C"]
    groups = [[a] for a in pm]
    for a in pc:
        g = [a]
        while co and len(g) < 4: g.append(co.pop(0))
        groups.append(g)
    while co: groups.append([co.pop(0) for _ in range(min(4, len(co)))])
    return groups # create 4 activity groups here
def solve_scenario(inst, scenario, time_limit=None, verbose=True):
    m = cp_model.CpModel() # the empty model
    base, W = CONFIG["week_base"], inst["W"]
    weeks = range(base, W + base)
    X, E, obj = {}, {}, [] # X [a,w] is a binary matrix on wheter that work in conducted in consider week or not
    for a in inst["A"]: # E[a, w] wil this week has ECLO # obj value of penality to minimise
        d = inst["act"][a] # here is the job record disctionary
        d["weeks"] = [w for w in weeks if w >= d["start_week"]] # legally avialble week indices for the activity
        if not d["weeks"]: return None, dict(status="HORIZON_TOO_SHORT", scenario=scenario)
        for w in d["weeks"]:
            X[a, w] = m.NewBoolVar(f"x_{a}_{w}")
            E[a, w] = m.NewBoolVar(f"e_{a}_{w}")
            m.AddImplication(E[a, w], X[a, w])
            if scenario == "A": m.Add(E[a, w] == 0)
        m.Add(sum(CONFIG["std_yield"] * X[a, w] + (CONFIG["eclo_yield"] - CONFIG["std_yield"]) * E[a, w] for w in d["weeks"]) >= math.ceil(CONFIG["std_yield"] * d["need"]))
    last = {}
    for a in inst["A"]:
        last[a] = m.NewIntVar(0, W + base, f"L_{a}")
        m.AddMaxEquality(last[a], [w * X[a, w] for w in inst["act"][a]["weeks"]])
    for a in inst["A"]:
        p = inst["act"][a]["pred"]
        if not p: continue
        for w in inst["act"][a]["weeks"]: m.Add(last[p] <= w - 1).OnlyEnforceIf(X[a, w])
    over = {}
    for a in inst["A"]:
        cn = inst["act"][a]["contract"]
        pw, gap = inst["planned_week"][cn], (week_end(inst, inst["planned_week"][cn]) - inst["planned_date"][cn]).days
        over[a] = m.NewIntVar(0, 7 * (W + base), f"o_{a}")
        m.Add(over[a] >= 7 * (last[a] - pw) + gap)
        if scenario == "B": m.Add(over[a] == 0)
    for (cn, ty), grp in inst["acts"].groupby(["contract_number", "activity_type"]):
        lim = inst["maxweek_access"][cn] * inst["wf"][cn]
        for w in weeks:
            terms = [X[a, w] for a in grp.index if (a, w) in X]
            if terms: m.Add(sum(terms) <= lim)
    loc_acts = defaultdict(list)
    for a in inst["A"]:
        for l in inst["act"][a]["footprint"]: loc_acts[l].append(a)
    for l, alist in loc_acts.items():
        hi = 0 if scenario == "A" else (CONFIG["c_excess_allowance"] if scenario == "C" else len(alist))
        for w in weeks:
            supply = supply_at(inst, l, w)
            terms = [(a, X[a, w]) for a in alist if (a, w) in X]
            if not terms: continue
            npm = [v for a, v in terms if inst["act"][a]["access"] == "PM"]
            npc = [v for a, v in terms if inst["act"][a]["access"] == "PC"]
            nc = [v for a, v in terms if inst["act"][a]["access"] == "C"]
            g = m.NewIntVar(0, len(terms), f"g_{l}_{w}")
            m.Add(g >= sum(npm) + sum(npc))
            m.Add(CONFIG["max_per_possession"] * g >= CONFIG["max_per_possession"] * sum(npm) + sum(npc) + sum(nc))
            ex = m.NewIntVar(0, hi, f"ex_{l}_{w}")
            m.Add(g <= supply + ex)
            if scenario != "A": obj.append(10 * CONFIG["w_excess"] * ex)
            bufx = [v for a, v in terms if has_buffer(inst, a)]
            if bufx:
                bflag = m.NewBoolVar(f"b_{l}_{w}")
                m.AddMaxEquality(bflag, bufx)
                m.Add(g <= 1).OnlyEnforceIf(bflag)
    for a, b in conflict_pairs(inst):
        for w in set(inst["act"][a]["weeks"]) & set(inst["act"][b]["weeks"]): m.Add(X[a, w] + X[b, w] <= 1)
    if scenario == "C":
        for line in inst["lines"].line_code:
            s = m.NewIntVar(base, W + base, f"win_{line}")
            for a in inst["A"]:
                if line not in inst["act"][a]["lines_affected"]: continue
                for w in inst["act"][a]["weeks"]:
                    m.Add(s <= w).OnlyEnforceIf(E[a, w])
                    m.Add(s >= w - (CONFIG["eclo_window_weeks"] - 1)).OnlyEnforceIf(E[a, w])
    if scenario != "A": obj += [10 * CONFIG["w_eclo"] * E[a, w] for a in inst["A"] for w in inst["act"][a]["weeks"]]
    if scenario != "B":
        for a in inst["A"]:
            d = inst["act"][a]
            obj.append(day_weight(inst, a) * over[a])
    obj += [CONFIG["tie_access"] * X[a, w] for a in inst["A"] for w in inst["act"][a]["weeks"]]
    obj += [CONFIG["tie_finish"] * last[a] for a in inst["A"]]
    m.Minimize(sum(obj))
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = time_limit or TIME_LIMIT
    sv.parameters.num_workers = CONFIG["workers"]
    st = sv.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE): return None, dict(status=sv.StatusName(st), scenario=scenario)
    if verbose: print(f"[{scenario}] CP-SAT {sv.StatusName(st)} obj={sv.ObjectiveValue():,.0f} bound={sv.BestObjectiveBound():,.0f} {sv.WallTime():.0f}s")
    plan = {a: sorted(w for w in inst["act"][a]["weeks"] if sv.Value(X[a, w])) for a in inst["A"]}
    eclo = {(a, w): int(sv.Value(E[a, w])) for a in inst["A"] for w in inst["act"][a]["weeks"] if sv.Value(X[a, w])}
    meta = dict(status=sv.StatusName(st), objective=sv.ObjectiveValue(), bound=sv.BestObjectiveBound(), seconds=sv.WallTime(), scenario=scenario)
    return build_submission(inst, scenario, plan, eclo), meta
def build_submission(inst, scenario, plan, eclo):
    nights = defaultdict(list)
    for a, ws in plan.items():
        for w in ws: nights[inst["act"][a]["contract"], inst["act"][a]["type"], w].append(a)
    night_of = {}
    for (cn, ty, w), alist in nights.items():
        for k, a in enumerate(sorted(alist)): night_of[a, w] = k // inst["wf"][cn] + 1
    access = []
    for a, ws in sorted(plan.items()):
        for seq, w in enumerate(ws, 1):
            access.append(dict(activity_id=a, access_seq=seq, week=w, eclo=eclo.get((a, w), 0), access_night=night_of[a, w]))
    present = defaultdict(list)
    for a, ws in plan.items():
        for w in ws:
            for l in inst["act"][a]["footprint"]: present[l, w].append(a)
    packed = {k: pack_groups(inst, sorted(v)) for k, v in present.items()}
    sets_by_week = defaultdict(set)
    for (l, w), gs in packed.items():
        for g in gs: sets_by_week[w].add(frozenset(g))
    label = {}
    for w, sets in sets_by_week.items():
        for i, s in enumerate(sorted(sets, key=lambda s: sorted(s)), 1): label[w, s] = f"b{i}"
    occ = []
    for (l, w), gs in sorted(packed.items()):
        for g in gs:
            for a in g: occ.append(dict(activity_id=a, week=w, location_id=l, co_share_group=label[w, frozenset(g)]))
    res = []
    for cn in inst["contracts"].index:
        acts = [a for a in inst["A"] if inst["act"][a]["contract"] == cn]
        lw = max((max(plan[a]) for a in acts if plan[a]), default=None)
        sim = week_end(inst, lw) if lw else None
        res.append(dict(scenario=scenario, contract_number=cn, simulated_completion_date=sim,
                        overrun_days=max(0, (sim - inst["planned_date"][cn]).days) if sim else 0))
    return dict(access=pd.DataFrame(access), occupancy=pd.DataFrame(occ), results=pd.DataFrame(res))
def write_submission(sub, outdir):
    os.makedirs(outdir, exist_ok=True)
    sub["access"].to_csv(os.path.join(outdir, "SCHEDULE_ACCESS.csv"), index=False)
    sub["occupancy"].to_csv(os.path.join(outdir, "SCHEDULE_OCCUPANCY.csv"), index=False)
    sub["results"].to_csv(os.path.join(outdir, "RESULTS.csv"), index=False)
    return outdir
def read_submission(outdir):
    return dict(access=pd.read_csv(os.path.join(outdir, "SCHEDULE_ACCESS.csv")), occupancy=pd.read_csv(os.path.join(outdir, "SCHEDULE_OCCUPANCY.csv")),
                results=pd.read_csv(os.path.join(outdir, "RESULTS.csv")))
def check_activity_rules(inst, plan, scen, add):
    for a in inst["A"]:
        d, rows = inst["act"][a], plan.get(a, [])
        yld = sum(1.5 if e else 1.0 for _, e, _ in rows)
        if yld + 1e-9 < d["need"]: add("workload", f"{a}: yield {yld} < required {d['need']}")
        if len({w for w, _, _ in rows}) != len(rows): add("workload", f"{a}: more than one access in the same week")
        for w, e, _ in rows:
            if w < d["start_week"]: add("start_date", f"{a}: week {w} before planned start week {d['start_week']}")
            if e and scen == "A": add("eclo", f"{a}: ECLO used in Scenario A (week {w})")
    for a in inst["A"]:
        p = inst["act"][a]["pred"]
        if not p or not plan.get(a) or not plan.get(p): continue
        if min(w for w, _, _ in plan[a]) <= max(w for w, _, _ in plan[p]):
            add("predecessor", f"{a} starts wk{min(w for w, _, _ in plan[a])} not after {p} finishes wk{max(w for w, _, _ in plan[p])}")
def check_contract_rules(inst, acc, add):
    tagged = acc.assign(cn=acc.activity_id.map(lambda a: inst["act"][a]["contract"]), ty=acc.activity_id.map(lambda a: inst["act"][a]["type"]))
    for (cn, ty, w), grp in tagged.groupby(["cn", "ty", "week"]):
        if grp.access_night.nunique() > inst["maxweek_access"][cn]: add("weekly", f"{cn}/{ty} wk{w}: {grp.access_night.nunique()} nights > cap {inst['maxweek_access'][cn]}")
        if (grp.access_night > inst["maxweek_access"][cn]).any(): add("weekly", f"{cn}/{ty} wk{w}: access_night index above cap")
        for n, g2 in grp.groupby("access_night"):
            if len(g2) > inst["wf"][cn]: add("workfront", f"{cn}/{ty} wk{w} night{n}: {len(g2)} activities > {inst['wf'][cn]} workfronts")
def check_location_rules(inst, occ, plan, scen, add):
    want = {(a, w): set(inst["act"][a]["footprint"]) for a in inst["A"] for w, _, _ in plan.get(a, [])}
    got = defaultdict(set)
    for r in occ.itertuples(): got[r.activity_id, int(r.week)].add(r.location_id)
    for k, locs in want.items():
        if got.get(k, set()) != locs:
            add("occupancy", f"{k[0]} wk{k[1]}: occupancy mismatch missing={sorted(locs - got.get(k, set()))[:3]} extra={sorted(got.get(k, set()) - locs)[:3]}")
    excess_total, hotspots = 0, []
    for (l, w), grp in occ.groupby(["location_id", "week"]):
        groups = grp.groupby("co_share_group").activity_id.apply(set)
        for gname, members in groups.items():
            kinds = [inst["act"][a]["access"] for a in members]
            if len(members) > CONFIG["max_per_possession"]: add("mix", f"{l} wk{w} {gname}: {len(members)} activities > {CONFIG['max_per_possession']}")
            if "PM" in kinds and len(members) > 1: add("mix", f"{l} wk{w} {gname}: PM shares possession")
            if kinds.count("PC") > 1: add("mix", f"{l} wk{w} {gname}: {kinds.count('PC')} PC in one possession")
        n, sup = len(groups), supply_at(inst, l, int(w))
        if n > sup:
            excess_total += n - sup
            hotspots.append(dict(location_id=l, week=int(w), possessions=n, supply=sup))
            if scen == "A" or (scen == "C" and n - sup > CONFIG["c_excess_allowance"]): add("capacity", f"{l} wk{w}: {n} possessions > supply {sup}")
        elif n == sup: hotspots.append(dict(location_id=l, week=int(w), possessions=n, supply=sup))
    return excess_total, hotspots
def check_closures(inst, occ, plan, add):
    grp_of = {(r.activity_id, int(r.week), r.location_id): r.co_share_group for r in occ.itertuples()}
    byweek = defaultdict(list)
    for a in inst["A"]:
        for w, _, _ in plan.get(a, []): byweek[w].append(a)
    for w, alist in byweek.items():
        for i in range(len(alist)):
            for j in range(i + 1, len(alist)):
                a, b = alist[i], alist[j]
                if not (has_buffer(inst, a) or has_buffer(inst, b)): continue
                fa, fb = set(inst["act"][a]["footprint"]), set(inst["act"][b]["footprint"])
                ca, cb = set(inst["act"][a]["closure"]), set(inst["act"][b]["closure"])
                shared = fa & fb
                same_group = shared and all(grp_of.get((a, w, l)) == grp_of.get((b, w, l)) for l in shared)
                hit = sorted((ca & cb) - (fa & fb)) if (same_group and co_shareable(inst, a, b)) else sorted(ca & cb)
                if not CONFIG["strict_cosharing"] and same_group and co_shareable(inst, a, b): hit = []
                if hit: add("closure", f"wk{w}: {b} inside closure of ['{a}'] at {hit[:2]}")
def validate(inst, sub):
    acc, occ, res = sub["access"], sub["occupancy"], sub["results"]
    scen = str(res.scenario.iloc[0]).strip().upper()
    v = []
    def add(rule, detail): v.append(dict(rule=rule, severity="hard", detail=detail))
    if res.scenario.nunique() > 1: add("planned_date", "RESULTS.csv mixes more than one scenario")
    plan = defaultdict(list)
    for r in acc.itertuples(): plan[r.activity_id].append((int(r.week), int(r.eclo), int(r.access_night)))
    check_activity_rules(inst, plan, scen, add)
    check_contract_rules(inst, acc, add)
    excess_total, hotspots = check_location_rules(inst, occ, plan, scen, add)
    check_closures(inst, occ, plan, add)
    eclo_nights = int(acc.eclo.sum())
    if scen == "C":
        for line in inst["lines"].line_code:
            ws = sorted({int(r.week) for r in acc.itertuples() if r.eclo and line in inst["act"][r.activity_id]["lines_affected"]})
            if ws and max(ws) - min(ws) + 1 > CONFIG["eclo_window_weeks"]: add("eclo_window", f"{line}: ECLO weeks {ws} span more than {CONFIG['eclo_window_weeks']} weeks")
    earliness, weighted, tiers = 0, 0.0, defaultdict(int)
    for r in res.itertuples():
        cn, od, sim = r.contract_number, int(r.overrun_days), pd.Timestamp(r.simulated_completion_date).date()
        real = max((week_end(inst, max(w for w, _, _ in plan[a])) for a in inst["A"] if inst["act"][a]["contract"] == cn and plan.get(a)), default=None)
        if real and real != sim: add("planned_date", f"{cn}: simulated_completion_date {sim} does not match schedule ({real})")
        if scen == "B" and od > 0: add("planned_date", f"{cn}: overrun {od}d not allowed in Scenario B")
        earliness += max(0, (inst["planned_date"][cn] - sim).days) if real else 0
    for r in res.itertuples(): tiers[inst["tier"][r.contract_number]] += int(r.overrun_days)
    for a in inst["A"]:
        if not plan.get(a): continue
        cn = inst["act"][a]["contract"]
        od = max(0, (week_end(inst, max(w for w, _, _ in plan[a])) - inst["planned_date"][cn]).days)
        weighted += day_weight(inst, a) / 10 * od
    soft = dict(scenario=scen, overrun_days_total=int(res.overrun_days.sum()), contracts_overrunning=int((res.overrun_days > 0).sum()),
                earliness_days_total=int(earliness), excess_access_nights_total=int(excess_total), eclo_nights_total=eclo_nights,
                priority_overrun={str(k): int(tiers.get(k, 0)) for k in (1, 2, 3)}, priority_weighted_score=round(weighted, 1))
    rep = dict(scenario=scen, feasible=not v, hard_violations=v, soft_scores=soft,
               detail=dict(capacity_hotspots=hotspots[:20], nights_scheduled=int(len(acc)), eclo_nights=eclo_nights))
    if not v:
        score = {"A": soft["priority_weighted_score"], "B": CONFIG["w_excess"] * soft["excess_access_nights_total"] + CONFIG["w_eclo"] * eclo_nights,
                 "C": soft["priority_weighted_score"] + CONFIG["w_excess"] * soft["excess_access_nights_total"] + CONFIG["w_eclo"] * eclo_nights}[scen]
        rep["soft_scores"]["objective_score"] = round(score, 1)
        rep["soft_scores"]["formula_version"] = "ps1-v1"
    return rep
def explain(inst, sub):
    acc, res = sub["access"], sub["results"]
    rows = []
    for r in res[res.overrun_days > 0].itertuples():
        cn = r.contract_number
        acts = [(a, int(acc[acc.activity_id == a].week.max())) for a in inst["A"] if inst["act"][a]["contract"] == cn and len(acc[acc.activity_id == a])]
        if not acts: continue
        drv, lw = max(acts, key=lambda t: t[1])
        d = inst["act"][drv]
        slack = inst["planned_week"][cn] - d["start_week"] + 1 - d["need"]
        cause = "workload exceeds weeks available before planned date" if slack < 0 else ("waiting on predecessor " + d["pred"] if d["pred"] else "contention at its locations pushed weeks later")
        rows.append(dict(contract=cn, priority=inst["tier"][cn], overrun_days=int(r.overrun_days), driving_activity=drv, last_week=lw,
                         planned_week=inst["planned_week"][cn], reason=cause, locations=", ".join(d["footprint"][:3])))
    return pd.DataFrame(rows).sort_values(["priority", "overrun_days"], ascending=[True, False]) if rows else pd.DataFrame(columns=["contract"])
def run_info(inst):
    print(f"{len(inst['A'])} activities, {inst['acts'].total_accesses.sum():g} access-nights, {len(inst['contracts'])} contracts, horizon {inst['horizon_weeks']} weeks from {inst['h0']} (+{CONFIG['extra_weeks']} overrun weeks)")
    print(f"{len(conflict_pairs(inst))} conflicting activity pairs; busiest locations:")
    demand = defaultdict(int)
    for a in inst["A"]:
        for l in inst["act"][a]["footprint"]: demand[l] += inst["act"][a]["need"]
    for l, n in sorted(demand.items(), key=lambda kv: -kv[1])[:8]: print(f"  {l:28s} demand {n:5.1f} access-nights, supply {supply_at(inst, l, CONFIG['week_base'])}/week")
def solve_with_growing_horizon(inst, scenario, time_limit):
    base_extra, base_W = CONFIG["extra_weeks"], inst["W"]
    try:
        while True:
            sub, meta = solve_scenario(inst, scenario, time_limit=time_limit)
            if sub is not None or meta["status"] not in ("INFEASIBLE", "HORIZON_TOO_SHORT"): return sub, meta
            if CONFIG["extra_weeks"] >= CONFIG["max_extra_weeks"]: return None, meta
            CONFIG["extra_weeks"] = min(CONFIG["extra_weeks"] * 2, CONFIG["max_extra_weeks"])
            inst["W"] = max(inst["W"], inst["horizon_weeks"] + CONFIG["extra_weeks"])
            print(f"[{scenario}] {meta['status']}: extending the horizon to {inst['W']} weeks and retrying")
    finally:
        CONFIG["extra_weeks"], inst["W"] = base_extra, base_W
def run_solve(inst, out, scenarios, time_limit):
    summary = []
    for s in scenarios:
        sub, meta = solve_with_growing_horizon(inst, s, time_limit)
        if sub is None:
            print(f"[{s}] no feasible schedule found ({meta['status']})")
            summary.append(dict(scenario=s, feasible=False, violations=None, note=meta["status"]))
            continue
        outdir = write_submission(sub, os.path.join(out, s))
        rep = validate(inst, sub)
        with open(os.path.join(outdir, "REPORT.json"), "w") as f: json.dump(rep, f, indent=2, default=str)
        explain(inst, sub).to_csv(os.path.join(outdir, "EXPLANATION.csv"), index=False)
        ss = rep["soft_scores"]
        print(f"[{s}] feasible={rep['feasible']} violations={len(rep['hard_violations'])} overrun_days={ss['overrun_days_total']} excess={ss['excess_access_nights_total']} eclo={ss['eclo_nights_total']} score={ss.get('objective_score')} -> {outdir}")
        for hv in rep["hard_violations"][:5]: print(f"     ! {hv['rule']}: {hv['detail']}")
        summary.append(dict(scenario=s, feasible=rep["feasible"], violations=len(rep["hard_violations"]),
                            **{k: ss[k] for k in ("overrun_days_total", "excess_access_nights_total", "eclo_nights_total", "priority_weighted_score")}, objective=ss.get("objective_score")))
    os.makedirs(out, exist_ok=True)
    pd.DataFrame(summary).to_csv(os.path.join(out, "SUMMARY.csv"), index=False)
    print(pd.DataFrame(summary).to_string(index=False))

if __name__ == "__main__":
    INSTANCE = load_instance(DATA_DIR)
    print(f"instance: {INSTANCE['path']}")

    if MODE == "info":
        run_info(INSTANCE)

    elif MODE == "validate":
        print(
            json.dumps(
                validate(
                    INSTANCE,
                    read_submission(VALIDATE_DIR)
                ),
                indent=2,
                default=str,
            )
        )

    else:
        run_solve(
            INSTANCE,
            OUT_DIR,
            SCENARIOS,
            TIME_LIMIT,
        )