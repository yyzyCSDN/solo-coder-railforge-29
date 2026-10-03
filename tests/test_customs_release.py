import unittest
from datetime import datetime, timezone

from railforge.api import RailForgeService
from railforge.cargo.segregation import Load
from railforge.customs.holds import Hold, active
from railforge.customs.releases import ReleaseEvent, released
from railforge.workflows.features import RailForgeFeatureService

U = timezone.utc
T = lambda *a: datetime(*a, tzinfo=U)


class OpenHoldTests(unittest.TestCase):
    def test_open_ended_hold_is_active(self):
        # No end time -> control stays open indefinitely.
        h = Hold(T(2026, 1, 1), None, "border/X", "inspection")
        self.assertEqual(active([h], "border/X", T(2026, 6, 1)), [h])

    def test_closed_hold_expires(self):
        h = Hold(T(2026, 1, 1), T(2026, 2, 1), "border/X", "inspection")
        self.assertEqual(active([h], "border/X", T(2026, 3, 1)), [])

    def test_scope_must_match(self):
        h = Hold(T(2026, 1, 1), None, "border/X", "inspection")
        self.assertEqual(active([h], "border/Y", T(2026, 6, 1)), [])


class ReleaseEventTests(unittest.TestCase):
    def test_latest_release_governs(self):
        rows = [ReleaseEvent("e1", "TR1", "border/X", "release", T(2026, 1, 2))]
        self.assertEqual(released(rows, "TR1", "border/X", T(2026, 1, 3)).event_id, "e1")

    def test_revocation_cancels_release(self):
        rows = [
            ReleaseEvent("e1", "TR1", "border/X", "release", T(2026, 1, 2)),
            ReleaseEvent("e2", "TR1", "border/X", "revoke", T(2026, 1, 4)),
        ]
        self.assertIsNone(released(rows, "TR1", "border/X", T(2026, 1, 5)))

    def test_future_release_not_in_force(self):
        rows = [ReleaseEvent("e1", "TR1", "border/X", "release", T(2026, 2, 1))]
        self.assertIsNone(released(rows, "TR1", "border/X", T(2026, 1, 1)))


class PreclearWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.svc = RailForgeService()
        self.feat: RailForgeFeatureService = self.svc.features
        self.check_at = T(2026, 1, 10)
        self.safe_loads = [Load("W1", "flammable", 1), Load("W2", "general", 20)]
        # Two flammable wagons too close -> forbidden distance 5 not met.
        self.bad_loads = [Load("W1", "flammable", 1), Load("W2", "flammable", 2)]
        self.rules = {frozenset({"flammable", "flammable"}): 5}
        self.release = [ReleaseEvent("rel-1", "TR1", "border/X", "release", T(2026, 1, 9))]

    def _state(self):
        return (self.svc.control.stream.head,
                self.svc.control.store.snapshot(),
                len(self.svc.control.trail._rows),
                self.svc.control.receipts)

    def assert_state_unchanged(self, before, op_id):
        after = self._state()
        self.assertEqual(before[0], after[0], "event stream must not grow on rejection")
        self.assertEqual(before[1], after[1], "versioned state must not change on rejection")
        self.assertEqual(before[2], after[2], "audit trail must not grow on rejection")
        self.assertNotIn(op_id, after[3], "no receipt may be recorded on rejection")

    def test_all_gates_pass_commits_atomically(self):
        r = self.feat.preclear_customs("op-ok", "TR1", [], "border/X", self.check_at,
                                       self.safe_loads, self.rules, self.release, self.check_at)
        self.assertEqual(r.result, {"precleared": True, "release_event": "rel-1"})
        self.assertEqual(r.event.payload["status"], "committed")
        self.assertEqual(r.event.payload["control_path"], "atomic")
        self.assertTrue(self.svc.control.stream.verify_chain())
        self.assertTrue(self.svc.control.trail.verify())

    def test_open_ended_hold_blocks(self):
        before = self._state()
        holds = [Hold(T(2026, 1, 1), None, "border/X", "open control")]
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-hold", "TR1", holds, "border/X", self.check_at,
                                       self.safe_loads, self.rules, self.release, self.check_at)
        self.assert_state_unchanged(before, "op-hold")

    def test_scope_mismatch_does_not_block_but_release_scope_must_match(self):
        # Hold is for another scope, so it does not apply; release is also for a
        # different scope -> must be rejected (release event gate).
        before = self._state()
        holds = [Hold(T(2026, 1, 1), None, "border/Y", "open control")]
        other_scope_release = [ReleaseEvent("rel-2", "TR1", "border/Y", "release", T(2026, 1, 9))]
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-scope", "TR1", holds, "border/X", self.check_at,
                                       self.safe_loads, self.rules, other_scope_release, self.check_at)
        self.assert_state_unchanged(before, "op-scope")

    def test_segregation_violation_blocks(self):
        before = self._state()
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-seg", "TR1", [], "border/X", self.check_at,
                                       self.bad_loads, self.rules, self.release, self.check_at)
        self.assert_state_unchanged(before, "op-seg")

    def test_missing_release_event_blocks(self):
        before = self._state()
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-rel", "TR1", [], "border/X", self.check_at,
                                       self.safe_loads, self.rules, [], self.check_at)
        self.assert_state_unchanged(before, "op-rel")

    def test_revoked_release_blocks(self):
        before = self._state()
        events = [
            ReleaseEvent("rel-1", "TR1", "border/X", "release", T(2026, 1, 8)),
            ReleaseEvent("rev-1", "TR1", "border/X", "revoke", T(2026, 1, 9, 12)),
        ]
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-rev", "TR1", [], "border/X", self.check_at,
                                       self.safe_loads, self.rules, events, self.check_at)
        self.assert_state_unchanged(before, "op-rev")

    def test_rejected_operation_can_later_commit(self):
        holds = [Hold(T(2026, 1, 1), None, "border/X", "open control")]
        with self.assertRaises(ValueError):
            self.feat.preclear_customs("op-retry", "TR1", holds, "border/X", self.check_at,
                                       self.safe_loads, self.rules, self.release, self.check_at)
        # Hold lifted (closed) for a later check -> commits.
        later_holds = [Hold(T(2026, 1, 1), T(2026, 1, 9), "border/X", "closed control")]
        r = self.feat.preclear_customs("op-retry", "TR1", later_holds, "border/X", self.check_at,
                                       self.safe_loads, self.rules, self.release, self.check_at)
        self.assertEqual(r.result["precleared"], True)


if __name__ == "__main__":
    unittest.main()
