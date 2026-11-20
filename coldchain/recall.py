"""Recall checks for mixed-loaded outbound shipments.

When the lot-level detail of an outbound month is lost, the warehouse still
knows, per recalled lot, how many *boxes* of that lot existed and which
outbound sequence numbers are *candidate* destinations of that lot. A check is
saved only when the boxes can be fully allocated:

* every lot's whole box count is distributed across its own candidate
  shipments (non-negative integer boxes per edge);
* every involved shipment receives exactly its frozen box count in total.

The problem is an integer-capacitated bipartite flow, so feasibility is
decided by maximum flow. For each (lot, shipment) candidate pair, the pair is
classified over *all* feasible allocations:

* ``always``  – positive in every feasible allocation;
* ``partial`` – positive in some, zero in others;
* ``never``   – zero in every feasible allocation.

Reported verifications pin edges to fixed values (zero allowed). Each new pin,
together with every previously saved pin, must leave a feasible allocation;
otherwise the submission is rejected and the stored check is untouched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from collections import deque
from datetime import datetime, timezone

from .db import transaction
from .errors import ConflictError, NotFoundError, ValidationError
from .timeutil import now_str, validate_month

MAX_RECALL_ROWS = 20
MAX_INVOLVED_SHIPMENTS = 20
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ERRORS = 40
MAX_LOT_LENGTH = 128

ALWAYS = "always"
PARTIAL = "partial"
NEVER = "never"
CONCLUSION_LABELS = {ALWAYS: "全部可行分配都含", PARTIAL: "仅部分可行分配含", NEVER: "肯定不含"}

_LOT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,%d}$" % MAX_LOT_LENGTH)
_SEQ_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_BOXES_RE = re.compile(r"^[0-9]{1,9}$")
_SPLIT_RE = re.compile(r"[|｜]")

FIELD_ALIASES = {
    "lot_no": ("批号", "lot", "lot_no", "batch", "batch_no", "批次号"),
    "boxes": ("箱数", "boxes", "boxcount", "box_count", "数量", "件数"),
    "candidates": (
        "候选流水号",
        "候选流水",
        "流水号",
        "candidates",
        "candidate_sequences",
        "sequences",
    ),
}
FIELD_LABELS = {"lot_no": "批号", "boxes": "箱数", "candidates": "候选流水号"}


# --------------------------------------------------------------------------- #
# Maximum flow (Dinic)                                                        #
# --------------------------------------------------------------------------- #


class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to, rev, cap):
        self.to = to
        self.rev = rev
        self.cap = cap


class Dinic:
    """Max flow on integer capacities. Nodes are integer indices."""

    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]

    def add_edge(self, fr, to, cap):
        forward = _Edge(to, len(self.graph[to]), cap)
        backward = _Edge(fr, len(self.graph[fr]), 0)
        self.graph[fr].append(forward)
        self.graph[to].append(backward)
        return forward

    def max_flow(self, source, sink):
        total = 0
        n = self.n
        graph = self.graph
        while True:
            level = [-1] * n
            level[source] = 0
            queue = deque([source])
            while queue:
                v = queue.popleft()
                for edge in graph[v]:
                    if edge.cap > 0 and level[edge.to] < 0:
                        level[edge.to] = level[v] + 1
                        queue.append(edge.to)
            if level[sink] < 0:
                return total
            iterator = [0] * n

            def augment(v, pushed):
                if v == sink:
                    return pushed
                while iterator[v] < len(graph[v]):
                    edge = graph[v][iterator[v]]
                    if edge.cap > 0 and level[edge.to] == level[v] + 1:
                        flow = augment(edge.to, min(pushed, edge.cap))
                        if flow:
                            edge.cap -= flow
                            graph[edge.to][edge.rev].cap += flow
                            return flow
                    iterator[v] += 1
                return 0

            while True:
                pushed = augment(source, 10**18)
                if not pushed:
                    break
                total += pushed


# --------------------------------------------------------------------------- #
# Feasibility and classification                                             #
# --------------------------------------------------------------------------- #


def _build_network(lots, shipments, edges, exact=None, floors=None):
    """Build a lower-bound feasible-flow network for one feasibility question.

    ``lots``      : list of (lot_id, boxes)
    ``shipments`` : list of (shipment_id, frozen_boxes)
    ``edges``     : list of (lot_id, shipment_id) candidate pairs
    ``exact``     : dict pair -> pinned integer boxes (the pair carries exactly
                    that many; zero forbids the pair entirely)
    ``floors``    : set of pairs that must carry at least one box

    Nodes: source -> lots -> shipments -> sink, plus a super source/sink for
    lower-bound demands. Lower bounds on lot->shipment edges are moved into
    node balances and satisfied by the super pair. Feasibility (see
    :func:`_satisfies`) requires that all demand edges saturate and that the
    ordinary source->sink flow then moves every lot box.

    Returns a dict consumed by :func:`_satisfies`.
    Raises ValidationError when pins alone exceed a node capacity.
    """
    exact = dict(exact or {})
    floors = set(floors or ())
    # Database rowids are not necessarily dense; assign dense node indices.
    lot_index = {lot_id: i for i, (lot_id, _) in enumerate(lots)}
    ship_offset = len(lots)
    ship_index = {
        ship_id: ship_offset + i for i, (ship_id, _) in enumerate(shipments)
    }
    source = ship_offset + len(shipments)
    sink = source + 1
    super_source = sink + 1
    super_sink = sink + 2
    dinic = Dinic(super_sink + 1)
    total_lot = sum(boxes for _, boxes in lots)
    lot_boxes = dict(lots)
    ship_boxes = dict(shipments)

    exact_lot = {lot_id: 0 for lot_id, _ in lots}
    exact_ship = {ship_id: 0 for ship_id, _ in shipments}
    for (lot_id, ship_id), value in exact.items():
        exact_lot[lot_id] += value
        exact_ship[ship_id] += value
    for lot_id, value in exact_lot.items():
        if value > lot_boxes[lot_id]:
            raise ValidationError(
                f"批号 {lot_id} 的实核箱数之和 {value} 已超过该批箱数 {lot_boxes[lot_id]}，"
                "不存在可行分配，未保存任何修改"
            )
    for ship_id, value in exact_ship.items():
        if value > ship_boxes[ship_id]:
            raise ValidationError(
                f"流水 {ship_id} 的实核箱数之和 {value} 已超过其冻结箱数 {ship_boxes[ship_id]}，"
                "不存在可行分配，未保存任何修改"
            )

    # Source->lot and shipment->sink keep full capacities; lower bounds on the
    # middle edges are balanced through the super pair instead.
    for lot_id, boxes in lots:
        dinic.add_edge(source, lot_index[lot_id], boxes)
    for ship_id, boxes in shipments:
        dinic.add_edge(ship_index[ship_id], sink, boxes)

    # balance[node] = lower-bound flow entering minus leaving.
    balance = [0] * dinic.n

    def add_lb_edge(u, v, lower, upper):
        dinic.add_edge(u, v, upper - lower)
        balance[u] -= lower
        balance[v] += lower

    for lot_id, ship_id in edges:
        u = lot_index[lot_id]
        v = ship_index[ship_id]
        if (lot_id, ship_id) in exact:
            pinned = exact[(lot_id, ship_id)]
            add_lb_edge(u, v, pinned, pinned)
        elif (lot_id, ship_id) in floors:
            add_lb_edge(u, v, 1, 10**15)
        else:
            dinic.add_edge(u, v, 10**15)

    # The return edge lets a forced super-source flow complete its path from a
    # shipment back to a lot when satisfying lower-bound demands.
    dinic.add_edge(sink, source, 10**15)

    demand_total = 0
    for node, demand in enumerate(balance):
        if demand > 0:
            dinic.add_edge(super_source, node, demand)
            demand_total += demand
        elif demand < 0:
            dinic.add_edge(node, super_sink, -demand)

    return {
        "dinic": dinic,
        "super_source": super_source,
        "super_sink": super_sink,
        "demand_total": demand_total,
        "total": total_lot,
        "source": source,
        "sink": sink,
    }


def _satisfies(built):
    """True when the lower-bound network admits a full-box allocation.

    First every forced lower-bound demand must be routable; then the ordinary
    source->sink flow must saturate all source->lot capacities, which means
    every lot box reaches a shipment (the shipment capacities pin the reverse
    direction, so totals match exactly).
    """
    dinic = built["dinic"]
    if dinic.max_flow(built["super_source"], built["super_sink"]) != built["demand_total"]:
        return False
    pushed = dinic.max_flow(built["source"], built["sink"])
    # The source->lot capacities always sum to the whole lot box count (lower
    # bounds are balanced through the super pair rather than removed here), so
    # saturating them means every box reached a shipment.
    return pushed == built["total"]


def feasible(lots, shipments, edges, fixed=None):
    """Return True when a complete integer allocation exists.

    ``fixed`` maps candidate pairs to pinned integer box counts (zero allowed).
    """
    if sum(boxes for _, boxes in lots) != sum(boxes for _, boxes in shipments):
        return False
    try:
        built = _build_network(lots, shipments, edges, exact=fixed)
    except ValidationError:
        return False
    return _satisfies(built)


def classify_edges(lots, shipments, edges, fixed=None):
    """Classify every candidate edge across all feasible allocations.

    Fixed edges are returned directly: a positive pin is ``always``, a zero
    pin is ``never``. Returns a dict (lot_id, shipment_id) -> conclusion.
    Raises ValidationError when no feasible allocation exists at all.
    """
    fixed = dict(fixed or {})
    if sum(boxes for _, boxes in lots) != sum(boxes for _, boxes in shipments):
        raise ValidationError(
            "各批号箱数之和与涉及流水的冻结箱数之和不一致，不存在可行分配，未保存任何修改"
        )
    if not feasible(lots, shipments, edges, fixed):
        raise ValidationError(
            "箱数无法在候选流水范围内全数分配（批号箱数须全部分给自身候选流水，"
            "每笔涉及流水须恰好分到冻结箱数），不存在可行分配，未保存任何修改"
        )

    result = {}
    for pair, value in fixed.items():
        # A pin is a hard constraint: positive is always, zero is never.
        result[pair] = NEVER if value == 0 else ALWAYS
    for pair in edges:
        if pair in fixed:
            continue
        # "Can be positive": impose a lower bound of one box on this pair.
        try:
            positive_built = _build_network(
                lots, shipments, edges, exact=fixed, floors={pair}
            )
        except ValidationError:
            can_be_positive = False
        else:
            can_be_positive = _satisfies(positive_built)
        # "Can be zero": pin the pair to zero explicitly.
        can_be_zero = feasible(lots, shipments, edges, {**fixed, pair: 0})
        if can_be_positive and not can_be_zero:
            result[pair] = ALWAYS
        elif can_be_positive and can_be_zero:
            result[pair] = PARTIAL
        else:
            result[pair] = NEVER
    return result


# --------------------------------------------------------------------------- #
# CSV parsing                                                                 #
# --------------------------------------------------------------------------- #


def _norm_header(value):
    text = (value or "").strip().lstrip("﻿").lower()
    return text.replace(" ", "").replace("_", "").replace("-", "")


def _decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("CSV 无法按 UTF-8 或 GB18030 解码，请另存为 UTF-8 后重试")


def _header_map(fieldnames):
    normalized = {}
    for index, name in enumerate(fieldnames):
        key = _norm_header(name)
        if key in normalized:
            raise ValidationError(f"CSV 表头重复：{name!r}")
        normalized[key] = (index, name)
    mapping = {}
    used = {}
    for field, aliases in FIELD_ALIASES.items():
        alias_set = {_norm_header(alias) for alias in aliases}
        matches = [(idx, name) for key, (idx, name) in normalized.items() if key in alias_set]
        if not matches:
            raise ValidationError(
                f"CSV 缺少必需列「{FIELD_LABELS[field]}」（可用列名：{' / '.join(aliases)}）"
            )
        if len(matches) > 1:
            names = "、".join(str(name) for _, name in matches)
            raise ValidationError(f"CSV 有多列都对应「{FIELD_LABELS[field]}」：{names}")
        idx, name = matches[0]
        if idx in used:
            raise ValidationError(f"表头列「{name}」被重复用于不同字段")
        used[idx] = field
        mapping[field] = idx
    if len(mapping) != 3:
        raise ValidationError("CSV 必须恰好包含批号、箱数、候选流水号三列")
    return mapping


def parse_recall_csv(raw):
    """Strictly parse a recall CSV. Returns rows of lot/candidates, no writes."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        raise ValidationError("上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError("CSV 超过 2 MiB 上限")
    sha256 = hashlib.sha256(raw).hexdigest()
    text = _decode(raw)

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 为空，缺少表头") from None
    if not any(str(cell).strip() for cell in header):
        raise ValidationError("CSV 首行表头为空")

    mapping = _header_map(header)
    width = len(header)
    errors = []
    rows = []
    seen_lots = {}

    for line_no, cells in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in cells):
            continue
        if len(rows) >= MAX_RECALL_ROWS:
            raise ValidationError(f"召回核查 CSV 最多 {MAX_RECALL_ROWS} 行（不含表头）")
        if len(cells) > width and any(str(c).strip() for c in cells[width:]):
            errors.append(f"第 {line_no} 行：数据列多于表头")
            if len(errors) >= MAX_ERRORS:
                break
            continue
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))

        def cell(field):
            return str(cells[mapping[field]]).strip()

        lot_no = cell("lot_no")
        boxes_text = cell("boxes")
        candidates_text = cell("candidates")
        row_ok = True

        if not _LOT_RE.match(lot_no):
            errors.append(f"第 {line_no} 行：批号为空或含控制字符或超过 {MAX_LOT_LENGTH} 字符")
            row_ok = False
        elif lot_no in seen_lots:
            errors.append(
                f"第 {line_no} 行：批号 {lot_no} 与第 {seen_lots[lot_no]} 行重复，批号必须唯一"
            )
            row_ok = False

        boxes = None
        if not _BOXES_RE.match(boxes_text) or int(boxes_text or "0") <= 0:
            errors.append(f"第 {line_no} 行：箱数必须为正整数")
            row_ok = False
        else:
            boxes = int(boxes_text)

        sequences = []
        if not candidates_text:
            errors.append(f"第 {line_no} 行：候选流水号为空")
            row_ok = False
        else:
            parts = [part.strip() for part in _SPLIT_RE.split(candidates_text) if part.strip()]
            if not parts:
                errors.append(f"第 {line_no} 行：候选流水号为空")
                row_ok = False
            else:
                row_seen = set()
                for seq in parts:
                    if not _SEQ_RE.match(seq):
                        errors.append(f"第 {line_no} 行：候选流水号 {seq!r} 含控制字符或超长")
                        row_ok = False
                    elif seq in row_seen:
                        errors.append(f"第 {line_no} 行：候选流水号 {seq} 在同一行重复")
                        row_ok = False
                    else:
                        row_seen.add(seq)
                        sequences.append(seq)

        if row_ok:
            seen_lots[lot_no] = line_no
            rows.append(
                {"lot_no": lot_no, "boxes": boxes, "candidates": sequences, "source_row": line_no}
            )

    if errors:
        more = "（仅显示前若干条）" if len(errors) >= MAX_ERRORS else ""
        raise ValidationError("CSV 校验未通过，未写入任何数据", details=errors + ([more] if more else []))
    if not rows:
        raise ValidationError("CSV 没有数据行")
    if len(rows) > MAX_RECALL_ROWS:
        raise ValidationError(f"召回核查 CSV 最多 {MAX_RECALL_ROWS} 行（不含表头）")
    distinct = {seq for row in rows for seq in row["candidates"]}
    if len(distinct) > MAX_INVOLVED_SHIPMENTS:
        raise ValidationError(
            f"涉及的候选流水去重后共 {len(distinct)} 笔，超过上限 {MAX_INVOLVED_SHIPMENTS} 笔"
        )
    return rows, sha256


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def _next_check_no(conn, created_at):
    """Generate a human-friendly, collision-free check number RC-YYYYMMDD-NNN."""
    stamp = created_at.strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM recall_checks WHERE check_no LIKE ?", (f"RC-{stamp}-%",)
    ).fetchone()
    return f"RC-{stamp}-{row['c'] + 1:03d}"


def _load_check_structure(conn, check_id):
    check = conn.execute("SELECT * FROM recall_checks WHERE id = ?", (check_id,)).fetchone()
    if check is None:
        raise NotFoundError(f"核查单 {check_id} 不存在")
    lots = conn.execute(
        "SELECT id, lot_no, boxes, row_order FROM recall_lots WHERE check_id = ? ORDER BY row_order",
        (check_id,),
    ).fetchall()
    candidates = conn.execute(
        "SELECT c.lot_id, c.shipment_id, c.sequence_no, c.frozen_boxes"
        " FROM recall_candidates c WHERE c.check_id = ? ORDER BY c.id",
        (check_id,),
    ).fetchall()
    verifications = conn.execute(
        "SELECT lot_id, shipment_id, verified_boxes, created_at"
        " FROM recall_verifications WHERE check_id = ? ORDER BY id",
        (check_id,),
    ).fetchall()
    return check, lots, candidates, verifications


def _structure_for_solver(lots, candidates):
    lot_pairs = [(row["id"], row["boxes"]) for row in lots]
    ship_map = {}
    edges = []
    for row in candidates:
        ship_map.setdefault(row["shipment_id"], row["frozen_boxes"])
        edges.append((row["lot_id"], row["shipment_id"]))
    shipments = list(ship_map.items())
    return lot_pairs, shipments, edges, ship_map


def create_check(conn, month, sku, raw):
    """Validate the upload against frozen facts and save only if feasible."""
    try:
        month = validate_month(month)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    sku = str(sku or "").strip()
    if not sku or re.search(r"[\x00-\x1f\x7f]", sku) or len(sku) > 64:
        raise ValidationError("SKU 为空、含控制字符或超过 64 字符")

    rows, sha256 = parse_recall_csv(raw)

    with transaction(conn):
        month_row = conn.execute(
            "SELECT month FROM months WHERE month = ?", (month,)
        ).fetchone()
        if month_row is None:
            raise ValidationError(f"月份 {month} 尚未冻结出库事实，不能创建召回核查")

        wanted = {seq for row in rows for seq in row["candidates"]}
        placeholders = ",".join("?" for _ in wanted)
        found_rows = conn.execute(
            f"SELECT id, sequence_no, sku, month, boxes FROM shipments"
            f" WHERE sequence_no IN ({placeholders})",
            tuple(wanted),
        ).fetchall()
        found = {row["sequence_no"]: row for row in found_rows}

        errors = []
        missing = sorted(wanted - set(found))
        if missing:
            errors.append("候选流水在冻结事实中不存在：" + "、".join(missing))
        wrong_month = sorted(
            row["sequence_no"] for row in found.values() if row["month"] != month
        )
        if wrong_month:
            errors.append(f"以下流水不属于所选月份 {month}：" + "、".join(wrong_month))
        wrong_sku = sorted(
            row["sequence_no"] for row in found.values() if row["sku"] != sku
        )
        if wrong_sku:
            errors.append(f"以下流水不属于所选 SKU {sku}：" + "、".join(wrong_sku))
        if errors:
            raise ValidationError("候选流水校验未通过，未写入任何数据", details=errors)

        created_at = now_str()
        created_dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        check_no = _next_check_no(conn, created_dt)
        try:
            cur = conn.execute(
                "INSERT INTO recall_checks(check_no, month, sku, created_at) VALUES(?,?,?,?)",
                (check_no, month, sku, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"核查单写入被拒绝：{exc}（未产生任何数据）") from exc
        check_id = cur.lastrowid

        lot_ids = {}
        lot_pairs = []
        for order, row in enumerate(rows):
            cur = conn.execute(
                "INSERT INTO recall_lots(check_id, lot_no, boxes, row_order) VALUES(?,?,?,?)",
                (check_id, row["lot_no"], row["boxes"], order),
            )
            lot_ids[row["lot_no"]] = cur.lastrowid
            lot_pairs.append((cur.lastrowid, row["boxes"]))

        ship_boxes = {}
        edges = []
        for row in rows:
            lot_id = lot_ids[row["lot_no"]]
            for seq in row["candidates"]:
                shipment = found[seq]
                ship_boxes[shipment["id"]] = shipment["boxes"]
                edges.append((lot_id, shipment["id"]))
                conn.execute(
                    "INSERT INTO recall_candidates"
                    "(check_id, lot_id, shipment_id, sequence_no, frozen_boxes)"
                    " VALUES(?,?,?,?,?)",
                    (check_id, lot_id, shipment["id"], seq, shipment["boxes"]),
                )
        shipments = list(ship_boxes.items())

        # Raises ValidationError (rolling back every insert above) when no
        # feasible allocation exists.
        conclusions = classify_edges(lot_pairs, shipments, edges)

        conn.executemany(
            "INSERT INTO recall_conclusions(check_id, lot_id, shipment_id, conclusion)"
            " VALUES(?,?,?,?)",
            [(check_id, lot_id, ship_id, conclusion) for (lot_id, ship_id), conclusion in conclusions.items()],
        )
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (
                created_at,
                "create_recall_check",
                month,
                None,
                f"{check_no} SKU {sku}：{len(rows)} 个批号 / {len(shipments)} 笔流水，sha256={sha256[:12]}",
            ),
        )

    return {
        "check_no": check_no,
        "check_id": check_id,
        "month": month,
        "sku": sku,
        "lot_count": len(rows),
        "shipment_count": len(shipments),
        "created_at": created_at,
    }


def add_verification(conn, check_no, lot_no, sequence_no, verified_boxes):
    """Pin one candidate edge to a verified box count; idempotent on same value."""
    check_no = str(check_no or "").strip()
    lot_no = str(lot_no or "").strip()
    sequence_no = str(sequence_no or "").strip()
    if isinstance(verified_boxes, bool):
        raise ValidationError("实核箱数必须是整数（允许填零）")
    try:
        value = int(str(verified_boxes).strip())
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("实核箱数必须是整数（允许填零）")
    if value < 0:
        raise ValidationError("实核箱数必须是非负整数（允许填零）")

    with transaction(conn):
        check = conn.execute(
            "SELECT * FROM recall_checks WHERE check_no = ?", (check_no,)
        ).fetchone()
        if check is None:
            raise NotFoundError(f"核查单 {check_no} 不存在")
        check_id = check["id"]

        lot = conn.execute(
            "SELECT id, boxes FROM recall_lots WHERE check_id = ? AND lot_no = ?",
            (check_id, lot_no),
        ).fetchone()
        if lot is None:
            raise ValidationError(f"批号 {lot_no} 不属于核查单 {check_no}")
        candidate = conn.execute(
            "SELECT shipment_id, frozen_boxes FROM recall_candidates"
            " WHERE check_id = ? AND lot_id = ? AND sequence_no = ?",
            (check_id, lot["id"], sequence_no),
        ).fetchone()
        if candidate is None:
            raise ValidationError(
                f"流水 {sequence_no} 不是批号 {lot_no} 在核查单 {check_no} 中的候选流水"
            )

        existing = conn.execute(
            "SELECT verified_boxes FROM recall_verifications"
            " WHERE check_id = ? AND lot_id = ? AND shipment_id = ?",
            (check_id, lot["id"], candidate["shipment_id"]),
        ).fetchone()
        if existing is not None:
            if existing["verified_boxes"] == value:
                # Idempotent re-submission: keep the original record, no write.
                return {
                    "check_no": check_no,
                    "lot_no": lot_no,
                    "sequence_no": sequence_no,
                    "verified_boxes": value,
                    "idempotent": True,
                }
            raise ConflictError(
                f"批号 {lot_no} 与流水 {sequence_no} 已保存实核箱数 "
                f"{existing['verified_boxes']}，与本次提交的 {value} 不一致；"
                "同一对候选流水的实核值不能更改，本次提交已拒绝，原核查结果保留"
            )

        _, lots, candidates, verifications = _load_check_structure(conn, check_id)
        lot_pairs, shipments, edges, _ = _structure_for_solver(lots, candidates)
        fixed = {
            (row["lot_id"], row["shipment_id"]): row["verified_boxes"] for row in verifications
        }
        fixed[(lot["id"], candidate["shipment_id"])] = value

        # Re-validate the whole set together; raises ValidationError if infeasible.
        conclusions = classify_edges(lot_pairs, shipments, edges, fixed)

        created_at = now_str()
        try:
            conn.execute(
                "INSERT INTO recall_verifications"
                "(check_id, lot_id, shipment_id, verified_boxes, created_at)"
                " VALUES(?,?,?,?,?)",
                (check_id, lot["id"], candidate["shipment_id"], value, created_at),
            )
            # Refresh the conclusion snapshot inside the same transaction.
            conn.execute("DELETE FROM recall_conclusions WHERE check_id = ?", (check_id,))
            conn.executemany(
                "INSERT INTO recall_conclusions(check_id, lot_id, shipment_id, conclusion)"
                " VALUES(?,?,?,?)",
                [
                    (check_id, lot_id, ship_id, conclusion)
                    for (lot_id, ship_id), conclusion in conclusions.items()
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"实核记录写入被拒绝：{exc}（原核查结果保留）") from exc
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (
                created_at,
                "add_recall_verification",
                check["month"],
                None,
                f"{check_no}：批号 {lot_no} / 流水 {sequence_no} 实核 {value} 箱",
            ),
        )

    return {
        "check_no": check_no,
        "lot_no": lot_no,
        "sequence_no": sequence_no,
        "verified_boxes": value,
        "idempotent": False,
    }


# --------------------------------------------------------------------------- #
# Reads                                                                       #
# --------------------------------------------------------------------------- #


def list_checks(conn):
    return conn.execute(
        "SELECT c.check_no, c.month, c.sku, c.created_at,"
        "       (SELECT COUNT(*) FROM recall_lots l WHERE l.check_id = c.id) AS lot_count,"
        "       (SELECT COUNT(DISTINCT r.shipment_id) FROM recall_candidates r"
        "         WHERE r.check_id = c.id) AS shipment_count,"
        "       (SELECT COUNT(*) FROM recall_verifications v WHERE v.check_id = c.id) AS verification_count"
        " FROM recall_checks c ORDER BY c.id DESC"
    ).fetchall()


def list_skus_for_month(conn, month):
    try:
        month = validate_month(month)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return conn.execute(
        "SELECT DISTINCT sku FROM shipments WHERE month = ? ORDER BY sku", (month,)
    ).fetchall()


def get_check_detail(conn, check_no):
    """Assemble the full, restart-safe view of one check."""
    check_no = str(check_no or "").strip()
    with transaction(conn, immediate=False):
        check = conn.execute(
            "SELECT * FROM recall_checks WHERE check_no = ?", (check_no,)
        ).fetchone()
        if check is None:
            raise NotFoundError(f"核查单 {check_no} 不存在")
        check_id = check["id"]
        lots = conn.execute(
            "SELECT id, lot_no, boxes, row_order FROM recall_lots"
            " WHERE check_id = ? ORDER BY row_order",
            (check_id,),
        ).fetchall()
        candidates = conn.execute(
            "SELECT lot_id, shipment_id, sequence_no, frozen_boxes"
            " FROM recall_candidates WHERE check_id = ? ORDER BY id",
            (check_id,),
        ).fetchall()
        verifications = conn.execute(
            "SELECT v.lot_id, v.shipment_id, v.verified_boxes, v.created_at,"
            "       c.sequence_no, l.lot_no"
            " FROM recall_verifications v"
            " JOIN recall_candidates c"
            "   ON c.check_id = v.check_id AND c.lot_id = v.lot_id AND c.shipment_id = v.shipment_id"
            " JOIN recall_lots l ON l.id = v.lot_id"
            " WHERE v.check_id = ? ORDER BY v.id",
            (check_id,),
        ).fetchall()
        conclusions = conn.execute(
            "SELECT lot_id, shipment_id, conclusion FROM recall_conclusions WHERE check_id = ?",
            (check_id,),
        ).fetchall()

    ship_by_id = {}
    for row in candidates:
        ship_by_id.setdefault(
            row["shipment_id"],
            {"shipment_id": row["shipment_id"], "sequence_no": row["sequence_no"],
             "frozen_boxes": row["frozen_boxes"]},
        )
    verified_map = {
        (row["lot_id"], row["shipment_id"]): row["verified_boxes"] for row in verifications
    }
    conclusion_map = {
        (row["lot_id"], row["shipment_id"]): row["conclusion"] for row in conclusions
    }

    lot_groups = []
    for lot in lots:
        pairs = []
        for row in candidates:
            if row["lot_id"] != lot["id"]:
                continue
            key = (lot["id"], row["shipment_id"])
            pairs.append(
                {
                    "lot_no": lot["lot_no"],
                    "lot_id": lot["id"],
                    "shipment_id": row["shipment_id"],
                    "sequence_no": row["sequence_no"],
                    "frozen_boxes": row["frozen_boxes"],
                    "conclusion": conclusion_map.get(key),
                    "conclusion_label": CONCLUSION_LABELS.get(conclusion_map.get(key), "—"),
                    "verified_boxes": verified_map.get(key),
                }
            )
        lot_groups.append(
            {
                "lot_id": lot["id"],
                "lot_no": lot["lot_no"],
                "boxes": lot["boxes"],
                "pairs": pairs,
            }
        )

    involved = sorted(ship_by_id.values(), key=lambda item: item["sequence_no"])
    buckets = {ALWAYS: [], PARTIAL: [], NEVER: []}
    for group in lot_groups:
        for pair in group["pairs"]:
            buckets[pair["conclusion"]].append(pair)

    return {
        "check": dict(check),
        "lot_groups": lot_groups,
        "involved_shipments": involved,
        "verifications": [dict(row) for row in verifications],
        "buckets": buckets,
        "conclusion_labels": CONCLUSION_LABELS,
    }


def check_json(detail):
    check = detail["check"]
    return {
        "check_no": check["check_no"],
        "month": check["month"],
        "sku": check["sku"],
        "created_at": check["created_at"],
        "lots": [
            {
                "lot_no": group["lot_no"],
                "boxes": group["boxes"],
                "candidates": [
                    {
                        "sequence_no": pair["sequence_no"],
                        "frozen_boxes": pair["frozen_boxes"],
                        "conclusion": pair["conclusion"],
                        "conclusion_label": pair["conclusion_label"],
                        "verified_boxes": pair["verified_boxes"],
                    }
                    for pair in group["pairs"]
                ],
            }
            for group in detail["lot_groups"]
        ],
        "involved_shipments": detail["involved_shipments"],
        "verifications": [
            {
                "lot_no": row["lot_no"],
                "sequence_no": row["sequence_no"],
                "verified_boxes": row["verified_boxes"],
                "created_at": row["created_at"],
            }
            for row in detail["verifications"]
        ],
        "conclusions": {
            key: [
                {
                    "lot_no": pair["lot_no"],
                    "sequence_no": pair["sequence_no"],
                    "frozen_boxes": pair["frozen_boxes"],
                    "verified_boxes": pair["verified_boxes"],
                }
                for pair in pairs
            ]
            for key, pairs in detail["buckets"].items()
        },
    }
