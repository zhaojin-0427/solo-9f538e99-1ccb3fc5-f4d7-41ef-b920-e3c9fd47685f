"""Recall check tests: feasibility, classification, verifications, durability."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from coldchain import recall
from coldchain.app import create_app
from coldchain.db import connect
from coldchain.errors import ConflictError, ValidationError


def month_csv(rows):
    lines = ["流水号,SKU,出库时刻,箱数"]
    lines.extend(f"{seq},SKU-A,2026-03-{day:02d}T01:00:00Z,{boxes}" for seq, day, boxes in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def recall_csv(text):
    return text.encode("utf-8")


class RecallAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "recall.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def freeze(self, shipments):
        response = self.client.post(
            "/api/months",
            data={"file": (io.BytesIO(month_csv(shipments)), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

    def create_check(self, body, month="2026-03", sku="SKU-A"):
        return self.client.post(
            "/api/recall/checks",
            data={
                "month": month,
                "sku": sku,
                "file": (io.BytesIO(recall_csv(body)), "r.csv"),
            },
            content_type="multipart/form-data",
        )

    def verify(self, check_no, lot_no, sequence_no, boxes):
        return self.client.post(
            f"/api/recall/checks/{check_no}/verifications",
            json={"lot_no": lot_no, "sequence_no": sequence_no, "verified_boxes": boxes},
        )

    def detail(self, check_no):
        return self.client.get(f"/api/recall/checks/{check_no}").get_json()

    def conclusion_map(self, detail):
        return {
            group["lot_no"] + "/" + pair["sequence_no"]: pair["conclusion"]
            for group in detail["lots"]
            for pair in group["candidates"]
        }


class SolverTests(unittest.TestCase):
    LOTS = [(1, 10), (2, 7)]
    SHIPS = [(10, 6), (11, 8), (12, 3)]
    EDGES = [(1, 10), (1, 11), (2, 11), (2, 12)]

    def test_unique_solution_classifies_all_always(self):
        result = recall.classify_edges(self.LOTS, self.SHIPS, self.EDGES)
        self.assertEqual(set(result.values()), {recall.ALWAYS})

    def test_partial_classification(self):
        lots = [(1, 5), (2, 5)]
        ships = [(10, 5), (11, 5)]
        edges = [(1, 10), (1, 11), (2, 10), (2, 11)]
        result = recall.classify_edges(lots, ships, edges)
        self.assertEqual(set(result.values()), {recall.PARTIAL})

    def test_never_when_ship_only_reachable_by_other_lot(self):
        # LOT-1 can only reach ship 10; ship 10 takes all of lot 1, so lot 2
        # can never enter it.
        lots = [(1, 4), (2, 6)]
        ships = [(10, 4), (11, 6)]
        edges = [(1, 10), (2, 10), (2, 11)]
        result = recall.classify_edges(lots, ships, edges)
        self.assertEqual(result[(2, 10)], recall.NEVER)
        self.assertEqual(result[(1, 10)], recall.ALWAYS)
        self.assertEqual(result[(2, 11)], recall.ALWAYS)

    def test_infeasible_when_totals_differ(self):
        self.assertFalse(recall.feasible([(1, 10)], [(10, 6)], [(1, 10)]))

    def test_structurally_infeasible(self):
        # Lot 1 can only reach ship 10 but ship 10 cannot hold all its boxes.
        self.assertFalse(
            recall.feasible(
                [(1, 10), (2, 10)], [(10, 15), (11, 5)], [(1, 10), (2, 11)]
            )
        )

    def test_pins_force_conclusions(self):
        result = recall.classify_edges(
            self.LOTS, self.SHIPS, self.EDGES, fixed={(1, 10): 6}
        )
        self.assertEqual(result[(1, 10)], recall.ALWAYS)
        fully_pinned = {
            (1, 10): 6, (2, 12): 3, (1, 11): 4, (2, 11): 4,
        }
        result = recall.classify_edges(self.LOTS, self.SHIPS, self.EDGES, fully_pinned)
        self.assertEqual(set(result.values()), {recall.ALWAYS})

    def test_infeasible_pin_raises(self):
        with self.assertRaises(ValidationError):
            recall.classify_edges(self.LOTS, self.SHIPS, self.EDGES, {(1, 10): 7})
        # Zero on the only edge reaching a ship makes the instance impossible.
        with self.assertRaises(ValidationError):
            recall.classify_edges(self.LOTS, self.SHIPS, self.EDGES, {(1, 10): 0})


class CsvParsingTests(unittest.TestCase):
    def test_valid_file(self):
        rows, _ = recall.parse_recall_csv(
            recall_csv("批号,箱数,候选流水号\nLOT-A,10,S1|S2\nLOT-B,7,S2｜S3\n")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["candidates"], ["S1", "S2"])
        self.assertEqual(rows[1]["candidates"], ["S2", "S3"])  # full-width pipe

    def test_row_limit(self):
        body = "批号,箱数,候选流水号\n" + "".join(f"L{i},1,S1\n" for i in range(21))
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv(body))

    def test_twenty_rows_allowed(self):
        body = "批号,箱数,候选流水号\n" + "".join(f"L{i},1,S1\n" for i in range(20))
        rows, _ = recall.parse_recall_csv(recall_csv(body))
        self.assertEqual(len(rows), 20)

    def test_involved_shipment_limit(self):
        seqs = "|".join(f"S{i}" for i in range(21))
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv(f"批号,箱数,候选流水号\nL1,1,{seqs}\n"))

    def test_duplicate_lot_rejected(self):
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv("批号,箱数,候选流水号\nL1,1,S1\nL1,2,S2\n"))

    def test_boxes_must_be_positive_integer(self):
        for bad in ("0", "-3", "1.5", "abc", ""):
            with self.assertRaises(ValidationError, msg=bad):
                recall.parse_recall_csv(recall_csv(f"批号,箱数,候选流水号\nL1,{bad},S1\n"))

    def test_duplicate_candidate_in_row_rejected(self):
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv("批号,箱数,候选流水号\nL1,3,S1|S1\n"))

    def test_missing_column_and_empty_candidates_rejected(self):
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv("批号,箱数\nL1,3\n"))
        with self.assertRaises(ValidationError):
            recall.parse_recall_csv(recall_csv("批号,箱数,候选流水号\nL1,3,|\n"))

    def test_gb18030_decoding(self):
        rows, _ = recall.parse_recall_csv(
            "批号,箱数,候选流水号\nL1,3,S1\n".encode("gb18030")
        )
        self.assertEqual(rows[0]["lot_no"], "L1")


class CheckCreationTests(RecallAppTestCase):
    SHIPMENTS = [("S1", 1, 6), ("S2", 2, 8), ("S3", 3, 3), ("B1", 4, 2)]

    def setUp(self):
        super().setUp()
        self.freeze(self.SHIPMENTS)

    def test_feasible_check_is_saved_with_number(self):
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,10,S1|S2\nLOT-B,7,S2|S3\n"
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertRegex(body["check_no"], r"^RC-\d{8}-\d{3}$")
        self.assertEqual(body["lot_count"], 2)
        self.assertEqual(body["shipment_count"], 3)
        listing = self.client.get("/api/recall/checks").get_json()["checks"]
        self.assertEqual([row["check_no"] for row in listing], [body["check_no"]])

    def test_infeasible_check_not_saved(self):
        # LOT-A 10 can only reach S1 which holds 6: impossible.
        response = self.create_check("批号,箱数,候选流水号\nLOT-A,10,S1\n")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/recall/checks").get_json()["checks"], [])

    def test_candidate_must_belong_to_month(self):
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,2,S1\n", month="2026-02"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("2026-02", response.get_json()["message"])

    def test_candidate_must_belong_to_sku(self):
        # Add a second SKU's shipment in the same frozen month.
        other = ("流水号,SKU,出库时刻,箱数\n"
                 "X1,SKU-Z,2026-03-05T01:00:00Z,2\n").encode()
        response = self.client.post(
            "/api/months",
            data={"file": (io.BytesIO(other), "z.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)  # month already frozen
        # Instead, freeze a separate month carrying SKU-Z.
        other_month = ("流水号,SKU,出库时刻,箱数\n"
                       "Z1,SKU-Z,2026-04-05T01:00:00Z,2\n").encode()
        self.assertEqual(
            self.client.post(
                "/api/months",
                data={"file": (io.BytesIO(other_month), "z.csv")},
                content_type="multipart/form-data",
            ).status_code,
            201,
        )
        # A SKU-A check cannot reference SKU-Z's sequence.
        response = self.create_check("批号,箱数,候选流水号\nLOT-A,2,Z1\n")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            any("SKU" in detail for detail in response.get_json()["details"])
        )

    def test_unknown_sequence_rejected(self):
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,2,GHOST\n"
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            any("不存在" in detail for detail in response.get_json()["details"])
        )

    def test_unfrozen_month_rejected(self):
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,2,S1\n", month="2026-01"
        )
        self.assertEqual(response.status_code, 400)

    def test_skus_endpoint(self):
        # The frozen month in this fixture only contains SKU-A.
        body = self.client.get("/api/recall/skus?month=2026-03").get_json()
        self.assertEqual(body["skus"], ["SKU-A"])

    def test_immutable_in_database(self):
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,10,S1|S2\nLOT-B,7,S2|S3\n"
        )
        self.assertEqual(response.status_code, 201)
        check_no = response.get_json()["check_no"]
        # Create a verification row so UPDATE/DELETE triggers have a matching row.
        self.assertEqual(
            self.verify(check_no, "LOT-A", "S1", 6).status_code, 201
        )
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE recall_lots SET boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM recall_checks")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE recall_verifications SET verified_boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM recall_candidates")
        finally:
            conn.close()


class VerificationTests(RecallAppTestCase):
    def setUp(self):
        super().setUp()
        self.freeze([("S1", 1, 6), ("S2", 2, 8), ("S3", 3, 3)])
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,10,S1|S2\nLOT-B,7,S2|S3\n"
        )
        self.assertEqual(response.status_code, 201)
        self.check_no = response.get_json()["check_no"]

    def test_zero_allowed_and_integers_required(self):
        # Build a separate check in its own frozen month where a zero edge is
        # genuinely feasible (the symmetric layout below).
        other_month = (
            "流水号,SKU,出库时刻,箱数\n"
            "T1,SKU-A,2026-04-01T01:00:00Z,5\n"
            "T2,SKU-A,2026-04-02T01:00:00Z,5\n"
        ).encode()
        self.assertEqual(
            self.client.post(
                "/api/months",
                data={"file": (io.BytesIO(other_month), "apr.csv")},
                content_type="multipart/form-data",
            ).status_code,
            201,
        )
        created = self.client.post(
            "/api/recall/checks",
            data={
                "month": "2026-04",
                "sku": "SKU-A",
                "file": (io.BytesIO(recall_csv(
                    "批号,箱数,候选流水号\nLOT-A,5,T1|T2\nLOT-B,5,T1|T2\n"
                )), "r.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 201)
        sym_no = created.get_json()["check_no"]
        # Zero is a legitimate integer and feasible in this symmetric layout.
        self.assertEqual(
            self.verify(sym_no, "LOT-A", "T2", 0).status_code, 201
        )
        # Non-integers are rejected against the original check.
        for bad in (-1, 1.5, "abc", None):
            self.assertEqual(
                self.verify(self.check_no, "LOT-B", "S3", bad).status_code, 400, bad
            )

    def test_same_value_is_idempotent(self):
        first = self.verify(self.check_no, "LOT-A", "S1", 6)
        self.assertEqual(first.status_code, 201)
        second = self.verify(self.check_no, "LOT-A", "S1", 6)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["idempotent"])
        detail = self.detail(self.check_no)
        self.assertEqual(len(detail["verifications"]), 1)

    def test_different_value_conflicts_and_keeps_original(self):
        self.assertEqual(self.verify(self.check_no, "LOT-A", "S1", 6).status_code, 201)
        response = self.verify(self.check_no, "LOT-A", "S1", 5)
        self.assertEqual(response.status_code, 409)
        detail = self.detail(self.check_no)
        self.assertEqual(detail["verifications"][0]["verified_boxes"], 6)

    def test_infeasible_verification_rejected_without_changing_conclusions(self):
        before = self.conclusion_map(self.detail(self.check_no))
        # S3 is only reachable through LOT-B, so pinning that pair to zero
        # makes the whole allocation impossible.
        response = self.verify(self.check_no, "LOT-B", "S3", 0)
        self.assertEqual(response.status_code, 400)
        after = self.conclusion_map(self.detail(self.check_no))
        self.assertEqual(before, after)
        self.assertEqual(self.detail(self.check_no)["verifications"], [])

    def test_each_verification_validated_with_saved_set(self):
        # LOT-A/S1 = 6 is feasible. Then LOT-A/S2 = 4 would over-fill lot A
        # (6 + 4 leaves nothing wrong by itself), but pin B/S3=3 and A/S2=4
        # forces B/S2=4: still feasible. Add an actually conflicting pin last.
        self.assertEqual(self.verify(self.check_no, "LOT-A", "S1", 6).status_code, 201)
        self.assertEqual(self.verify(self.check_no, "LOT-B", "S3", 3).status_code, 201)
        # Now LOT-A must put its remaining 4 on S2, and S2 then needs 4 from B;
        # pinning LOT-B/S2 to 3 breaks the S2 total (needs 8).
        response = self.verify(self.check_no, "LOT-B", "S2", 3)
        self.assertEqual(response.status_code, 400)
        detail = self.detail(self.check_no)
        self.assertEqual(len(detail["verifications"]), 2)

    def test_unknown_lot_and_non_candidate_rejected(self):
        self.assertEqual(
            self.verify(self.check_no, "NOPE", "S1", 1).status_code, 400
        )
        # S1 is not a candidate of LOT-B.
        self.assertEqual(
            self.verify(self.check_no, "LOT-B", "S1", 1).status_code, 400
        )

    def test_unknown_check_returns_404(self):
        self.assertEqual(
            self.client.post(
                "/api/recall/checks/RC-00000000-999/verifications",
                json={"lot_no": "LOT-A", "sequence_no": "S1", "verified_boxes": 1},
            ).status_code,
            404,
        )

    def test_conclusions_update_after_verification(self):
        initial = self.conclusion_map(self.detail(self.check_no))
        self.assertEqual(initial["LOT-A/S1"], recall.ALWAYS)
        self.verify(self.check_no, "LOT-A", "S1", 6)
        updated = self.detail(self.check_no)
        # The pinned pair is recorded as a verification and stays always.
        pinned = next(
            pair
            for group in updated["lots"]
            for pair in group["candidates"]
            if group["lot_no"] == "LOT-A" and pair["sequence_no"] == "S1"
        )
        self.assertEqual(pinned["verified_boxes"], 6)
        self.assertEqual(pinned["conclusion"], recall.ALWAYS)


class DurabilityTests(RecallAppTestCase):
    def test_restart_reopens_check_with_records_and_conclusions(self):
        self.freeze([("S1", 1, 5), ("S2", 2, 5)])
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,5,S1|S2\nLOT-B,5,S1|S2\n"
        )
        check_no = response.get_json()["check_no"]
        self.verify(check_no, "LOT-A", "S1", 2)
        before = self.detail(check_no)

        restarted = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = restarted.test_client()
        after = client.get(f"/api/recall/checks/{check_no}").get_json()
        self.assertEqual(after["check_no"], check_no)
        self.assertEqual(
            [(v["lot_no"], v["sequence_no"], v["verified_boxes"]) for v in after["verifications"]],
            [(v["lot_no"], v["sequence_no"], v["verified_boxes"]) for v in before["verifications"]],
        )
        self.assertEqual(
            self._conclusions(after), self._conclusions(before)
        )
        # Pages render after restart too.
        self.assertEqual(client.get("/recall").status_code, 200)
        self.assertEqual(client.get(f"/recall/{check_no}").status_code, 200)

    @staticmethod
    def _conclusions(detail):
        return {
            group["lot_no"] + "/" + pair["sequence_no"]: pair["conclusion"]
            for group in detail["lots"]
            for pair in group["candidates"]
        }

    def test_parallel_verifications_serialize_and_keep_feasibility(self):
        self.freeze([("S1", 1, 5), ("S2", 2, 5)])
        response = self.create_check(
            "批号,箱数,候选流水号\nLOT-A,5,S1|S2\nLOT-B,5,S1|S2\n"
        )
        check_no = response.get_json()["check_no"]
        barrier = threading.Barrier(2)
        outcomes = []

        def submit(lot_no, sequence_no, boxes):
            conn = connect(self.db_path)
            barrier.wait()
            try:
                recall.add_verification(conn, check_no, lot_no, sequence_no, boxes)
                outcomes.append("ok")
            except (ValidationError, ConflictError):
                outcomes.append("rejected")
            finally:
                conn.close()

        # The same pair submitted concurrently with two different values:
        # BEGIN IMMEDIATE serializes the writers, one value wins and the other
        # is rejected rather than both being written.
        threads = [
            threading.Thread(target=submit, args=("LOT-A", "S1", 2)),
            threading.Thread(target=submit, args=("LOT-A", "S1", 3)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        detail = self.detail(check_no)
        saved = {
            (v["lot_no"], v["sequence_no"]): v["verified_boxes"]
            for v in detail["verifications"]
        }
        # The conflicting pair holds exactly one of the two submitted values.
        self.assertIn(saved.get(("LOT-A", "S1")), (2, 3))
        self.assertEqual(len(detail["verifications"]), 1)
        # The surviving pin set still admits a feasible allocation.
        lot_ids = {group["lot_no"]: index + 1 for index, group in enumerate(detail["lots"])}
        ship_ids = {
            ship["sequence_no"]: 100 + index
            for index, ship in enumerate(detail["involved_shipments"])
        }
        lot_pairs = [
            (lot_ids[group["lot_no"]], group["boxes"]) for group in detail["lots"]
        ]
        ship_pairs = [
            (ship_ids[ship["sequence_no"]], ship["frozen_boxes"])
            for ship in detail["involved_shipments"]
        ]
        edges = [
            (lot_ids[group["lot_no"]], ship_ids[pair["sequence_no"]])
            for group in detail["lots"]
            for pair in group["candidates"]
        ]
        fixed = {
            (lot_ids[group["lot_no"]], ship_ids[pair["sequence_no"]]): pair["verified_boxes"]
            for group in detail["lots"]
            for pair in group["candidates"]
            if pair["verified_boxes"] is not None
        }
        self.assertTrue(recall.feasible(lot_pairs, ship_pairs, edges, fixed))


class HtmlFlowTests(RecallAppTestCase):
    def test_pages_and_form_submission(self):
        self.freeze([("S1", 1, 5), ("S2", 2, 5)])
        self.assertEqual(self.client.get("/recall").status_code, 200)
        response = self.client.post(
            "/recall",
            data={
                "month": "2026-03",
                "sku": "SKU-A",
                "file": (io.BytesIO(
                    recall_csv("批号,箱数,候选流水号\nLOT-A,5,S1|S2\nLOT-B,5,S1|S2\n")
                ), "r.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        check_no = response.headers["Location"].rstrip("/").split("/")[-1]
        page = self.client.get(f"/recall/{check_no}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("全部可行分配都含".encode(), page.data)
        verify = self.client.post(
            f"/recall/{check_no}/verify",
            data={"lot_no": "LOT-A", "sequence_no": "S1", "verified_boxes": "2"},
        )
        self.assertEqual(verify.status_code, 302)


if __name__ == "__main__":
    unittest.main()
