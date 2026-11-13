"""Tests for shared.timing module."""

import concurrent.futures
import logging

import pytest

from shared.timing import ModuleResult, RunTiming, get_timing, reset_timing, timed


class TestTimed:
    def test_timed_records_success(self):
        @timed("test_module")
        def succeeding():
            return 42

        result = succeeding()
        assert result == 42
        timing = get_timing()
        assert "test_module" in timing.modules
        assert timing.modules["test_module"].status == "success"
        assert timing.modules["test_module"].duration_seconds >= 0
        assert timing.modules["test_module"].error is None

    def test_timed_records_error(self):
        @timed("failing_module")
        def failing():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            failing()

        timing = get_timing()
        assert "failing_module" in timing.modules
        assert timing.modules["failing_module"].status == "error"
        assert timing.modules["failing_module"].error == "test error"
        assert timing.modules["failing_module"].duration_seconds >= 0

    def test_timed_preserves_function_name(self):
        @timed("my_func")
        def my_function():
            pass

        assert my_function.__name__ == "my_function"

    def test_timed_passes_args_and_kwargs(self):
        @timed("args_test")
        def add(a, b, extra=0):
            return a + b + extra

        assert add(1, 2, extra=3) == 6

    def test_timed_records_warning_on_internal_error_log(self):
        """Function returns normally but logs an ERROR -> status is 'warning'."""
        test_logger = logging.getLogger("test.inner_module")

        @timed("warning_module")
        def internally_failing():
            test_logger.error("Something broke inside")
            return "default_value"

        result = internally_failing()
        assert result == "default_value"
        timing = get_timing()
        assert timing.modules["warning_module"].status == "warning"
        assert "Something broke inside" in timing.modules["warning_module"].error

    def test_timed_success_with_info_log_not_warning(self):
        """Function logs INFO (not ERROR) -> status stays 'success'."""
        test_logger = logging.getLogger("test.info_module")

        @timed("info_module")
        def noisy_but_ok():
            test_logger.info("Just some info")
            return 42

        result = noisy_but_ok()
        assert result == 42
        timing = get_timing()
        assert timing.modules["info_module"].status == "success"

    def test_timed_warning_thread_isolation(self):
        """ERROR logged on thread A should not affect module running on thread B."""
        error_logger = logging.getLogger("test.thread_iso")

        @timed("module_a")
        def module_a():
            error_logger.error("Module A had an error")
            return "a"

        @timed("module_b")
        def module_b():
            return "b"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fa = executor.submit(module_a)
            fb = executor.submit(module_b)
            fa.result()
            fb.result()

        timing = get_timing()
        assert timing.modules["module_a"].status == "warning"
        assert timing.modules["module_b"].status == "success"

    def test_timed_handler_cleanup_on_exception(self):
        """Handler is removed even when the function raises."""
        root = logging.getLogger()
        handler_count_before = len(root.handlers)

        @timed("cleanup_test")
        def raiser():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            raiser()

        assert len(root.handlers) == handler_count_before

    def test_timed_multiple_error_logs_joined(self):
        """Multiple ERROR logs are joined with semicolons in the error field."""
        test_logger = logging.getLogger("test.multi_error")

        @timed("multi_error")
        def multi_fail():
            test_logger.error("First problem")
            test_logger.error("Second problem")
            return "degraded"

        multi_fail()
        timing = get_timing()
        mod = timing.modules["multi_error"]
        assert mod.status == "warning"
        assert "First problem" in mod.error
        assert "Second problem" in mod.error

    def test_timed_captures_errors_from_child_threads(self):
        """ERROR logged in a ContextAwareExecutor child thread is captured by parent @timed."""
        from shared.context_executor import ContextAwareExecutor

        child_logger = logging.getLogger("test.child_thread")

        @timed("nested_module")
        def module_with_children():
            with ContextAwareExecutor(max_workers=2) as executor:

                def child_task():
                    child_logger.error("Child thread error")

                executor.submit(child_task).result()
            return "done"

        result = module_with_children()
        assert result == "done"
        timing = get_timing()
        assert timing.modules["nested_module"].status == "warning"
        assert "Child thread error" in timing.modules["nested_module"].error

    def test_timed_plain_executor_does_not_capture_child_errors(self):
        """Plain ThreadPoolExecutor child thread errors are NOT captured (no context propagation)."""
        child_logger = logging.getLogger("test.plain_child")

        @timed("plain_nested")
        def module_with_plain_children():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:

                def child_task():
                    child_logger.error("Plain child error")

                executor.submit(child_task).result()
            return "done"

        result = module_with_plain_children()
        assert result == "done"
        timing = get_timing()
        assert timing.modules["plain_nested"].status == "success"


class TestRunTiming:
    def test_record_and_summary(self):
        timing = RunTiming()
        timing.record(
            ModuleResult(name="weather", status="success", duration_seconds=1.234)
        )
        timing.record(
            ModuleResult(
                name="trails", status="error", duration_seconds=0.5, error="timeout"
            )
        )

        summary = timing.summary()
        assert summary["weather"]["status"] == "success"
        assert summary["weather"]["duration_seconds"] == 1.23
        assert summary["weather"]["error"] is None
        assert summary["trails"]["status"] == "error"
        assert summary["trails"]["error"] == "timeout"

    def test_empty_summary(self):
        timing = RunTiming()
        assert timing.summary() == {}

    def test_abandon_records_error_and_ignores_late_result(self):
        timing = RunTiming()
        timing.abandon("weather", 300, "timed out after 300s")
        timing.record(
            ModuleResult(name="weather", status="success", duration_seconds=2923.4)
        )

        summary = timing.summary()
        assert summary["weather"]["status"] == "error"
        assert summary["weather"]["error"] == "timed out after 300s"
        assert summary["weather"]["duration_seconds"] == 300


class TestGetTiming:
    def test_get_timing_creates_singleton(self):
        t1 = get_timing()
        t2 = get_timing()
        assert t1 is t2

    def test_reset_timing_clears(self):
        t1 = get_timing()
        reset_timing()
        t2 = get_timing()
        assert t1 is not t2
