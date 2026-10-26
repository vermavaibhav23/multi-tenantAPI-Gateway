import unittest

from benchmarks.run import summarize_measurement


class MeasurementTests(unittest.TestCase):
    def test_drain_time_and_errors_cannot_inflate_successful_throughput(self):
        metrics = {
            'completion_elapsed_ms': {'values': {'max': 75000}},
            'completed_requests': {'values': {'count': 5900}},
            'successful_requests': {'values': {'count': 5800}},
            'valid_responses': {'values': {'rate': 5800 / 5900}},
            'rate_limited': {'values': {'rate': 0}},
            'server_errors': {'values': {'count': 80}},
            'transport_errors': {'values': {'count': 20}},
            'dropped_iterations': {'values': {'count': 100}},
            'successful_duration': {'values': {'p(95)': 123, 'p(99)': 456}},
            'all_request_duration': {'values': {'p(95)': 789}},
            'request_start_epoch_ms': {'values': {'min': 1700000000000}},
            'request_finish_epoch_ms': {'values': {'max': 1700000075000}},
            # Built-in HTTP counts also include warmup and must not be used.
            'http_reqs': {'values': {'count': 6000}},
        }
        row = summarize_measurement({'metrics': metrics}, 100, 60, 'test', 1000, 99)
        self.assertAlmostEqual(row['successful_rps'], 5800 / 75)
        self.assertAlmostEqual(row['offered_work_completed_fraction'], 5800 / 6000)
        self.assertEqual(row['completed_requests'], 5900)
        self.assertAlmostEqual(row['invalid_response_fraction'], 100 / 5900)
        self.assertEqual(row['server_errors'], 80)
        self.assertEqual(row['transport_errors'], 20)
        self.assertEqual(row['dropped_iterations'], 100)
        self.assertEqual(row['p95_success_ms'], 123)
        self.assertEqual(row['p95_all_request_ms'], 789)
        self.assertEqual(row['measurement_started_at'], '2023-11-14T22:13:20+00:00')
        self.assertEqual(row['measurement_finished_at'], '2023-11-14T22:14:35+00:00')
        self.assertFalse(row['thresholds_passed'])

    def test_empty_run_is_not_successful(self):
        row = summarize_measurement({'metrics': {}}, 100, 60, 'empty', 1000, 0)
        self.assertEqual(row['successful_rps'], 0)
        self.assertEqual(row['invalid_response_fraction'], 1)
        self.assertIsNone(row['p95_success_ms'])
        self.assertFalse(row['thresholds_passed'])


if __name__ == '__main__':
    unittest.main()
