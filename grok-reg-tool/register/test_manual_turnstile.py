import unittest

from manual_turnstile import wait_for_manual_turnstile


class ManualTurnstileTests(unittest.TestCase):
    def test_waits_until_user_completes_challenge(self):
        responses = iter(["", "", "verified-token"])
        logs = []
        sleeps = []

        token = wait_for_manual_turnstile(
            lambda: next(responses),
            timeout=10,
            interval=1,
            sleep=lambda seconds: sleeps.append(seconds),
            log=logs.append,
            takeover_url="http://127.0.0.1:6080/vnc.html",
        )

        self.assertEqual(token, "verified-token")
        self.assertEqual(sleeps, [1, 1])
        self.assertTrue(any("等待人工完成 Turnstile" in line for line in logs))
        self.assertTrue(any("http://127.0.0.1:6080/vnc.html" in line for line in logs))
        self.assertTrue(any("人工验证已通过" in line for line in logs))

    def test_times_out_with_clear_error(self):
        now_values = iter([0, 1, 2, 3, 4])

        with self.assertRaisesRegex(TimeoutError, "人工 Turnstile 验证等待超时"):
            wait_for_manual_turnstile(
                lambda: "",
                timeout=3,
                interval=1,
                sleep=lambda _seconds: None,
                monotonic=lambda: next(now_values),
                log=lambda _line: None,
            )


if __name__ == "__main__":
    unittest.main()
