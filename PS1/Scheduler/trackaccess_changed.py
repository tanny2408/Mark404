import argparse, json, os, datetime as dt
from collections import defaultdict
import pandas as pd
from ortools.sat.python import cp_model

CONFIG = dict(

    platform_mode="junction",

    buffer_scope="week",

    week_base=1,
    extra_weeks=8,

    time_limit=180, workers=8,

    eclo_yield=3, std_yield=2,

    w_excess=7,
    w_eclo=5,
    tier_weight={1: 100, 2: 10, 3: 1},
    act_nudge={1: 3, 2: 2, 3: 0},
    c_excess_allowance=1,
    eclo_window_weeks=2,

    tie_access=1, tie_finish=1,

    churn_weight=2)

def find_data_dir(path):

    if os.path.isfile(os.path.join(path, "08_ACTIVITY_DETAILS.csv")): return path
    for root, _, files in os.walk(path):
        if "08_ACTIVITY_DETAILS.csv" in files: return root
    raise SystemExit(f"could not find 08_ACTIVITY_DETAILS.csv under '{path}' - point --data at the instance folder")

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
    return [l for l in dict.fromkeys(locs) if l in inst["cap"]]

def expand(inst, start_loc, end_loc, nature):

    p1, p2 = start_loc.split(":"), end_loc.split(":")
    line, bound = p1[1], p1[3]
    i, j = inst["secpos"][line][":".join(p1[:3])], inst["secpos"][line][":".join(p2[:3])]
    if i > j: i, j = j, i
    footprint = span_locations(inst, line, bound, i, j)
    nb, mirror = inst["buf"].get(nature, (0, 0))
    closure = set(footprint)
    if nb:
        closure |= set(span_locations(inst, line, bound, max(0, i - nb), min(len(inst["seclist"][line]) - 1, j + nb)))
    if mirror:
        ob = "WB" if bound == "EB" else "EB"
        closure |= {l[:-2] + ob for l in list(closure)}

        if any(":H01_H02:" in l or f"PLAT:{line}:H01:" in l or f"PLAT:{line}:H02:" in l for l in closure):
            other = [x for x in inst["lines"].line_code if x != line][0]
            for b in ("EB", "WB"): closure |= {f"SEC:{other}:H01_H02:{b}", f"PLAT:{other}:H01:{b}", f"PLAT:{other}:H02:{b}"}
    return footprint, sorted(l for l in closure if l in inst["cap"])

def check_cycles(act):

    seen, stack = set(), set()
    def walk(x):
        if x in stack: raise SystemExit(f"predecessor cycle at {x}")
        if x in seen: return
        stack.add(x); seen.add(x)
        if act[x]["pred"]: walk(act[x]["pred"])
        stack.discard(x)
    for a in act: walk(a)

def load_instance(path):

    path = find_data_dir(path)
    rd = lambda f: pd.read_csv(os.path.join(path, f))
    params_df = rd("06_PARAMETERS.csv")
    inst = dict(path=path, lines=rd("01_LINES.csv"), stations=rd("02_STATIONS.csv"), sectors=rd("03_SECTORS.csv"),
                supply=rd("04_LOCATION_SUPPLY.csv"), buffers=rd("05_BUFFER_LOCATION.csv"), params=dict(zip(params_df.key, params_df.value)),
                contracts=rd("07_PROJECT_DETAILS.csv").set_index("contract_number"), acts=rd("08_ACTIVITY_DETAILS.csv").set_index("activity_id"))
    inst["h0"] = pd.Timestamp(inst["params"]["horizon_start"]).date()
    inst["horizon_weeks"] = int(inst["params"]["horizon_weeks"])
    inst["W"] = inst["horizon_weeks"] + CONFIG["extra_weeks"]

    inst["cap"] = dict(zip(inst["supply"].location_id, inst["supply"].supply_capacity))
    inst["buf"] = {r.nature_of_works: (int(r.up_to_buffer_sectors), int(r.opposite_bound_required)) for r in inst["buffers"].itertuples()}

    inst["seclist"] = {l: list(inst["sectors"][inst["sectors"].line_code == l].sort_values("seq").sector_id) for l in inst["lines"].line_code}
    inst["secpos"] = {l: {s: i for i, s in enumerate(v)} for l, v in inst["seclist"].items()}
    inst["secrow"] = inst["sectors"].set_index("sector_id")
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
        inst["act"][aid] = dict(id=aid, contract=cn, type=r.activity_type, need=int(r.total_accesses), prio=int(r.activity_priority),
                                start_week=week_of(inst, r.planned_start_date), pred=None if pd.isna(r.predecessor_activity_id) else r.predecessor_activity_id,
                                nature=inst["nature"][cn], access=inst["access_type"][cn], footprint=fp, closure=cl,
                                line=r.start_location_id.split(":")[1], bound=r.start_location_id.split(":")[3],

                                lines={l.split(":")[1] for l in cl})
    for aid, a in inst["act"].items():
        if a["pred"] and a["pred"] not in inst["act"]: raise SystemExit(f"{aid}: unknown predecessor {a['pred']}")
    check_cycles(inst["act"])
    return inst

def has_buffer(inst, a):

    return inst["buf"].get(inst["act"][a]["nature"], (0, 0))[0] > 0 or inst["act"][a]["access"] == "PM"

def co_shareable(inst, a, b):

    ta, tb = inst["act"][a]["access"], inst["act"][b]["access"]
    return ta != "PM" and tb != "PM" and not (ta == "PC" and tb == "PC")

def conflict_pairs(inst):

    out, A = [], inst["A"]
    for x in range(len(A)):
        for y in range(x + 1, len(A)):
            a, b = A[x], A[y]
            if not (has_buffer(inst, a) or has_buffer(inst, b)): continue
            fa, fb = set(inst["act"][a]["footprint"]), set(inst["act"][b]["footprint"])
            ca, cb = set(inst["act"][a]["closure"]), set(inst["act"][b]["closure"])
            overlap = bool(fa & fb)
            if overlap and co_shareable(inst, a, b): continue
            if overlap or (ca & fb) or (cb & fa) or ((ca - fa) & (cb - fb)): out.append((a, b))
    return out

def pack_groups(inst, acts_here, night_of=None, w=None):

    if any(has_buffer(inst, a) for a in acts_here): return [list(acts_here)]
    def conflict_key(a):
        return (inst["act"][a]["contract"], inst["act"][a]["type"], night_of.get((a, w)) if night_of is not None else None)
    def compatible(g, a):
        ka = conflict_key(a)
        return all(conflict_key(b)[:2] != ka[:2] or conflict_key(b)[2] == ka[2] for b in g)
    pm = [a for a in acts_here if inst["act"][a]["access"] == "PM"]
    pc = [a for a in acts_here if inst["act"][a]["access"] == "PC"]
    co = [a for a in acts_here if inst["act"][a]["access"] == "C"]
    groups = [[a] for a in pm]
    for a in pc:
        g = [a]
        i = 0
        while i < len(co) and len(g) < 4:
            if compatible(g, co[i]): g.append(co.pop(i))
            else: i += 1
        groups.append(g)
    while co:
        g = [co.pop(0)]
        i = 0
        while i < len(co) and len(g) < 4:
            if compatible(g, co[i]): g.append(co.pop(i))
            else: i += 1
        groups.append(g)
    return groups

def relaxed_warm_start(inst):

    order, seen = [], set()
    def visit(a):
        if a in seen: return
        seen.add(a)
        if inst["act"][a]["pred"]: visit(inst["act"][a]["pred"])
        order.append(a)
    for a in inst["A"]: visit(a)
    finish, hint = {}, {}
    for a in order:
        d = inst["act"][a]
        start = max(d["start_week"], finish[d["pred"]] + 1) if d["pred"] else d["start_week"]
        span = max(1, d["need"])
        ws = list(range(start, start + span))
        for w in ws: hint[a, w] = 1
        finish[a] = ws[-1]
    return hint

def solve_scenario(inst, scenario, time_limit=None, verbose=True, baseline=None, churn_weight=None):

    m = cp_model.CpModel()
    base, W = CONFIG["week_base"], inst["W"]
    weeks = range(base, W + base)
    X, E, obj = {}, {}, []

    for a in inst["A"]:
        d = inst["act"][a]
        d["weeks"] = [w for w in weeks if w >= d["start_week"]]
        if not d["weeks"]: raise SystemExit(f"{a}: planned start week is past the horizon, raise --extra-weeks")
        for w in d["weeks"]:
            X[a, w] = m.NewBoolVar(f"x_{a}_{w}")
            E[a, w] = m.NewBoolVar(f"e_{a}_{w}")
            m.AddImplication(E[a, w], X[a, w])
            if scenario == "A": m.Add(E[a, w] == 0)

        m.Add(sum(CONFIG["std_yield"] * X[a, w] + (CONFIG["eclo_yield"] - CONFIG["std_yield"]) * E[a, w] for w in d["weeks"]) >= CONFIG["std_yield"] * d["need"])

    sum_weeks_all = sum(len(inst["act"][a]["weeks"]) for a in inst["A"])
    max_tie = CONFIG["tie_access"] * sum_weeks_all + CONFIG["tie_finish"] * len(inst["A"]) * (W + base)
    churn_guard = max_tie + 1
    cw = (churn_weight if churn_weight is not None else CONFIG["churn_weight"]) if baseline is not None else 0
    max_churn = cw * sum_weeks_all
    tie_guard = churn_guard * max_churn + max_tie + 1

    seed = relaxed_warm_start(inst)
    for (a, w), val in seed.items():
        if (a, w) in X: m.AddHint(X[a, w], val)

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
        pw = inst["planned_week"][inst["act"][a]["contract"]]
        over[a] = m.NewIntVar(0, W + base, f"o_{a}")
        m.Add(over[a] >= last[a] - pw)
        if scenario == "B": m.Add(over[a] == 0)

    if baseline is not None:
        for a in inst["A"]:
            base_weeks = set(baseline.get(a, []))
            for w in inst["act"][a]["weeks"]:
                obj.append(churn_guard * cw * ((1 - X[a, w]) if w in base_weeks else X[a, w]))

    for (cn, ty), grp in inst["acts"].groupby(["contract_number", "activity_type"]):
        lim = inst["maxweek_access"][cn] * inst["wf"][cn]
        for w in weeks:
            terms = [X[a, w] for a in grp.index if (a, w) in X]
            if terms: m.Add(sum(terms) <= lim)
    loc_acts = defaultdict(list)
    for a in inst["A"]:
        for l in inst["act"][a]["footprint"]: loc_acts[l].append(a)
    for l, alist in loc_acts.items():
        supply = int(inst["cap"][l])

        hi = 0 if scenario == "A" else (CONFIG["c_excess_allowance"] if scenario == "C" else len(alist))
        for w in weeks:
            terms = [(a, X[a, w]) for a in alist if (a, w) in X]
            if not terms: continue
            npm = [v for a, v in terms if inst["act"][a]["access"] == "PM"]
            npc = [v for a, v in terms if inst["act"][a]["access"] == "PC"]
            nc = [v for a, v in terms if inst["act"][a]["access"] == "C"]

            g = m.NewIntVar(0, len(terms), f"g_{l}_{w}")
            m.Add(g >= sum(npm) + sum(npc))
            m.Add(4 * g >= 4 * sum(npm) + sum(npc) + sum(nc))
            ex = m.NewIntVar(0, hi, f"ex_{l}_{w}")
            m.Add(g <= supply + ex)
            if scenario != "A": obj.append(tie_guard * 10 * CONFIG["w_excess"] * ex)

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
                if line not in inst["act"][a]["lines"]: continue
                for w in inst["act"][a]["weeks"]:
                    m.Add(s <= w).OnlyEnforceIf(E[a, w])
                    m.Add(s >= w - (CONFIG["eclo_window_weeks"] - 1)).OnlyEnforceIf(E[a, w])

    if scenario != "A": obj += [tie_guard * 10 * CONFIG["w_eclo"] * E[a, w] for a in inst["A"] for w in inst["act"][a]["weeks"]]
    if scenario != "B":
        for a in inst["A"]:
            d = inst["act"][a]
            wgt10 = CONFIG["tier_weight"][inst["tier"][d["contract"]]] * (10 + CONFIG["act_nudge"][d["prio"]])
            obj.append(tie_guard * wgt10 * 7 * over[a])

    obj += [CONFIG["tie_access"] * X[a, w] for a in inst["A"] for w in inst["act"][a]["weeks"]]
    obj += [CONFIG["tie_finish"] * last[a] for a in inst["A"]]
    m.Minimize(sum(obj))
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = time_limit or CONFIG["time_limit"]
    sv.parameters.num_workers = CONFIG["workers"]
    st = sv.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE): return None, dict(status=sv.StatusName(st), scenario=scenario)
    if verbose: print(f"[{scenario}] CP-SAT {sv.StatusName(st)} obj≈{sv.ObjectiveValue()/(10*tie_guard):,.1f} bound≈{sv.BestObjectiveBound()/(10*tie_guard):,.1f} {sv.WallTime():.0f}s  (≈real score scale; tie-breakers/churn excluded)")
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
    occ = []
    for (l, w), alist in sorted(present.items()):
        for gi, g in enumerate(pack_groups(inst, sorted(alist)), 1):
            for a in g: occ.append(dict(activity_id=a, week=w, location_id=l, co_share_group=f"b{gi}"))

    res = []
    for cn in inst["contracts"].index:
        acts = [a for a in inst["A"] if inst["act"][a]["contract"] == cn]
        lw = max((max(plan[a]) for a in acts if plan[a]), default=None)
        sim = week_end(inst, lw) if lw else None
        res.append(dict(scenario=scenario, contract_number=cn, simulated_completion_date=sim,
                        overrun_days=max(0, (sim - inst["planned_date"][cn]).days) if sim else 0))
    return dict(access=pd.DataFrame(access), occupancy=pd.DataFrame(occ), results=pd.DataFrame(res))

def solve_with_autoextend(inst, scenario, time_limit=None, verbose=True, baseline=None, churn_weight=None, max_extra_weeks=64):

    tried = CONFIG["extra_weeks"]
    while True:
        inst["W"] = inst["horizon_weeks"] + tried
        sub, meta = solve_scenario(inst, scenario, time_limit=time_limit, verbose=verbose, baseline=baseline, churn_weight=churn_weight)
        if sub is not None:
            if tried != CONFIG["extra_weeks"] and verbose: print(f"[{scenario}] needed +{tried} overrun weeks (default was +{CONFIG['extra_weeks']}) to become feasible")
            return sub, meta
        if meta.get("status") != "INFEASIBLE":
            if verbose: print(f"[{scenario}] solve returned {meta.get('status')} at +{tried} weeks - a search/time issue, not a horizon issue; raise --time-limit to keep trying at this size instead of extending further")
            return sub, meta
        if tried >= max_extra_weeks:
            if verbose: print(f"[{scenario}] proven infeasible even after extending overrun room to +{tried} weeks")
            return sub, meta
        tried = min(tried * 2, max_extra_weeks)

def write_submission(sub, outdir):

    os.makedirs(outdir, exist_ok=True)
    sub["access"].to_csv(os.path.join(outdir, "SCHEDULE_ACCESS.csv"), index=False)
    sub["occupancy"].to_csv(os.path.join(outdir, "SCHEDULE_OCCUPANCY.csv"), index=False)
    sub["results"].to_csv(os.path.join(outdir, "RESULTS.csv"), index=False)
    return outdir

def read_submission(outdir):

    return dict(access=pd.read_csv(os.path.join(outdir, "SCHEDULE_ACCESS.csv")), occupancy=pd.read_csv(os.path.join(outdir, "SCHEDULE_OCCUPANCY.csv")),
                results=pd.read_csv(os.path.join(outdir, "RESULTS.csv")))

def replan(inst, scenario, cap_overrides, baseline_sub, time_limit=None, churn_weight=None):

    unknown = [l for l in cap_overrides if l not in inst["cap"]]
    if unknown: print(f"replan: ignoring unknown location_id(s) {unknown}")
    overrides = {l: c for l, c in cap_overrides.items() if l in inst["cap"]}
    saved = {l: inst["cap"][l] for l in overrides}
    inst["cap"].update(overrides)
    baseline_plan = {a: sorted(int(w) for w in g.week) for a, g in baseline_sub["access"].groupby("activity_id")}
    try:
        sub, meta = solve_with_autoextend(inst, scenario, time_limit=time_limit, baseline=baseline_plan, churn_weight=churn_weight)
    finally:
        inst["cap"].update(saved)
    if sub is None: return sub, meta, None
    new_plan = {a: sorted(int(w) for w in g.week) for a, g in sub["access"].groupby("activity_id")}
    moved = sorted(a for a in inst["A"] if new_plan.get(a, []) != baseline_plan.get(a, []))
    return sub, meta, moved

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
    return None

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
            if len(members) > 4: add("mix", f"{l} wk{w} {gname}: {len(members)} activities > 4")
            if "PM" in kinds and len(members) > 1: add("mix", f"{l} wk{w} {gname}: PM shares possession")
            if kinds.count("PC") > 1: add("mix", f"{l} wk{w} {gname}: {kinds.count('PC')} PC in one possession")
        n, sup = len(groups), int(inst["cap"][l])
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
                if same_group and co_shareable(inst, a, b): continue
                hit = sorted((ca & fb) | (cb & fa) | (shared if not co_shareable(inst, a, b) else set()) | ((ca - fa) & (cb - fb)))
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
            ws = sorted({int(r.week) for r in acc.itertuples() if r.eclo and line in inst["act"][r.activity_id]["lines"]})
            if ws and max(ws) - min(ws) + 1 > CONFIG["eclo_window_weeks"]: add("eclo_window", f"{line}: ECLO weeks {ws} span more than {CONFIG['eclo_window_weeks']} weeks")
    earliness, weighted, tiers = 0, 0.0, defaultdict(int)
    for r in res.itertuples():
        cn, od, sim = r.contract_number, int(r.overrun_days), pd.Timestamp(r.simulated_completion_date).date()
        real = max((week_end(inst, max(w for w, _, _ in plan[a])) for a in inst["A"] if inst["act"][a]["contract"] == cn and plan.get(a)), default=None)
        if real and real != sim: add("planned_date", f"{cn}: simulated_completion_date {sim} does not match schedule ({real})")

        expected_od = max(0, (sim - inst["planned_date"][cn]).days)
        if od != expected_od: add("planned_date", f"{cn}: overrun_days {od} does not match its own simulated_completion_date {sim} (expected {expected_od})")
        if scen == "B" and od > 0: add("planned_date", f"{cn}: overrun {od}d not allowed in Scenario B")
        earliness += max(0, (inst["planned_date"][cn] - sim).days) if real else 0

        tiers[inst["tier"][cn]] += od
    for a in inst["A"]:
        if not plan.get(a): continue
        cn = inst["act"][a]["contract"]
        od = max(0, (week_end(inst, max(w for w, _, _ in plan[a])) - inst["planned_date"][cn]).days)

        weighted += CONFIG["tier_weight"][inst["tier"][cn]] * (1 + CONFIG["act_nudge"][inst["act"][a]["prio"]] / 10) * od
    soft = dict(scenario=scen, overrun_days_total=int(res.overrun_days.sum()), contracts_overrunning=int((res.overrun_days > 0).sum()),
                earliness_days_total=int(earliness), excess_access_nights_total=int(excess_total), eclo_nights_total=eclo_nights,
                priority_overrun={str(k): int(tiers.get(k, 0)) for k in (1, 2, 3)}, priority_weighted_score=round(weighted, 1))
    rep = dict(scenario=scen, feasible=not v, hard_violations=v, soft_scores=soft,
               detail=dict(capacity_hotspots=hotspots[:20], nights_scheduled=int(len(acc)), eclo_nights=eclo_nights))
    if not v:
        score = {"A": soft["priority_weighted_score"], "B": CONFIG["w_excess"] * soft["excess_access_nights_total"] + CONFIG["w_eclo"] * eclo_nights,
                 "C": soft["priority_weighted_score"] + CONFIG["w_excess"] * soft["excess_access_nights_total"] + CONFIG["w_eclo"] * eclo_nights}[scen]
        rep["soft_scores"]["objective_score"] = round(score, 1)
        rep["soft_scores"]["formula_version"] = "ps1-v4"
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

    print(f"{len(inst['A'])} activities, {inst['acts'].total_accesses.sum()} access-nights, {len(inst['contracts'])} contracts, horizon {inst['horizon_weeks']} weeks from {inst['h0']} (+{CONFIG['extra_weeks']} overrun weeks)")
    print(f"{len(conflict_pairs(inst))} conflicting activity pairs; busiest locations:")
    demand = defaultdict(int)
    for a in inst["A"]:
        for l in inst["act"][a]["footprint"]: demand[l] += inst["act"][a]["need"]
    for l, n in sorted(demand.items(), key=lambda kv: -kv[1])[:8]: print(f"  {l:28s} demand {n:3d} access-nights, supply {inst['cap'][l]}/week")

def run_solve(inst, out, scenarios, time_limit):

    summary = []
    for s in scenarios:
        sub, meta = solve_with_autoextend(inst, s, time_limit=time_limit)
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

def main():

    ap = argparse.ArgumentParser(description="Track access scheduler (PS1). Run with no arguments to solve A, B and C using the CSVs in the current folder.")
    ap.add_argument("cmd", nargs="?", default="solve", choices=["solve", "validate", "info", "replan"], help="solve (default), validate, info or replan")
    ap.add_argument("--data", default=".", help="folder holding the 8 instance CSVs, searched recursively (default: current folder)")
    ap.add_argument("--out", default="out", help="folder to write submissions into (default: out)")
    ap.add_argument("--scenarios", default="A,B,C")
    ap.add_argument("--time-limit", type=float, default=None, help="seconds per scenario")
    ap.add_argument("--platform-mode", choices=["junction", "inclusive"], default=None, help="how platform sectors are booked along a span")
    ap.add_argument("--extra-weeks", type=int, default=None, help="starting overrun-week allowance; auto-doubles on infeasibility up to 64 (see solve_with_autoextend)")
    ap.add_argument("--submission", default=None, help="folder of an existing submission to validate or replan from")
    ap.add_argument("--replan-cap", action="append", default=[], metavar="LOCATION_ID=NEWCAP",
                     help="repeatable: override a location's capacity for replanning, e.g. SEC:ALP:S02_S03:EB=1")
    args = ap.parse_args()
    if args.platform_mode: CONFIG["platform_mode"] = args.platform_mode
    if args.extra_weeks is not None: CONFIG["extra_weeks"] = args.extra_weeks
    inst = load_instance(args.data)
    print(f"instance: {inst['path']}")
    if args.cmd == "info": run_info(inst)
    elif args.cmd == "validate": print(json.dumps(validate(inst, read_submission(args.submission or args.out)), indent=2, default=str))
    elif args.cmd == "replan":
        scen = args.scenarios.split(",")[0].strip().upper()
        baseline = read_submission(args.submission or os.path.join(args.out, scen))
        overrides = {}
        for kv in args.replan_cap:
            loc, cap = kv.split("=")
            overrides[loc] = int(cap)
        sub, meta, moved = replan(inst, scen, overrides, baseline, time_limit=args.time_limit)
        if sub is None:
            print(f"replan infeasible: {meta}")
        else:
            outdir = write_submission(sub, os.path.join(args.out, scen + "_replan"))
            tail = f": {moved[:10]}{'...' if len(moved) > 10 else ''}" if moved else ""
            print(f"replanned [{scen}] -> {outdir}; {len(moved)} activities changed{tail}")
    else: run_solve(inst, args.out, [x.strip().upper() for x in args.scenarios.split(",")], args.time_limit)

if __name__ == "__main__":
    main()
