import unittest

from homelab import NotificationActionRouter


class NotificationActionRouterTests(unittest.TestCase):
    def test_builds_action_id(self):
        self.assertEqual(
            NotificationActionRouter.make_action("HOMELAB_SRE_APPROVE", "token-123"),
            "HOMELAB_SRE_APPROVE::token-123",
        )

    def test_routes_matching_mobile_action(self):
        routed = []
        router = NotificationActionRouter()
        router.register("HOMELAB_SRE_APPROVE", lambda value, event: routed.append((value, event)))

        event = {
            "event_type": "mobile_app_notification_action",
            "data": {"action": "HOMELAB_SRE_APPROVE::opaque-token"},
        }

        self.assertTrue(router.handle_event(event))
        self.assertEqual(routed, [("opaque-token", event)])

    def test_ignores_unrelated_and_malformed_events(self):
        router = NotificationActionRouter()
        router.register("HOMELAB_SRE_APPROVE", lambda value, event: self.fail("should not route"))

        self.assertFalse(router.handle_event({"event_type": "state_changed"}))
        self.assertFalse(router.handle_event({"event_type": "mobile_app_notification_action"}))
        self.assertFalse(router.handle_event({"event_type": "mobile_app_notification_action", "data": {}}))
        self.assertFalse(
            router.handle_event(
                {
                    "event_type": "mobile_app_notification_action",
                    "data": {"action": "OTHER_SERVICE::token"},
                }
            )
        )

    def test_rejects_empty_action_parts(self):
        with self.assertRaisesRegex(ValueError, "prefix is required"):
            NotificationActionRouter.make_action(" ", "token")

        with self.assertRaisesRegex(ValueError, "value is required"):
            NotificationActionRouter.make_action("PREFIX", " ")

        with self.assertRaisesRegex(ValueError, "must not contain"):
            NotificationActionRouter.make_action("BAD::PREFIX", "token")
