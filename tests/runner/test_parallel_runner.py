import sys
from concurrent.futures.process import ProcessPoolExecutor
from typing import Any, Dict

import pytest

from kedro.framework.hooks import _create_hook_manager
from kedro.io import (
    AbstractDataSet,
    DataCatalog,
    DataSetError,
    LambdaDataSet,
    MemoryDataSet,
)
from kedro.pipeline import Pipeline, node
from kedro.runner import ParallelRunner
from kedro.runner.parallel_runner import (
    _MAX_WINDOWS_WORKERS,
    ParallelRunnerManager,
    _run_node_synchronization,
    _SharedMemoryDataSet,
)
from tests.runner.conftest import (
    exception_fn,
    identity,
    return_none,
    return_not_serializable,
    sink,
    source,
)


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Due to bug in parallel runner"
)
class TestValidParallelRunner:
    def test_create_default_data_set(self):
        # data_set is a proxy to a dataset in another process.
        data_set = ParallelRunner().create_default_data_set("")
        assert isinstance(data_set, _SharedMemoryDataSet)

    @pytest.mark.parametrize("is_async", [False, True])
    def test_parallel_run(self, is_async, fan_out_fan_in, catalog):
        catalog.add_feed_dict(dict(A=42))
        result = ParallelRunner(is_async=is_async).run(fan_out_fan_in, catalog)
        assert "Z" in result
        assert len(result["Z"]) == 3
        assert result["Z"] == (42, 42, 42)

    @pytest.mark.parametrize("is_async", [False, True])
    def test_parallel_run_with_plugin_manager(self, is_async, fan_out_fan_in, catalog):
        catalog.add_feed_dict(dict(A=42))
        result = ParallelRunner(is_async=is_async).run(
            fan_out_fan_in, catalog, hook_manager=_create_hook_manager()
        )
        assert "Z" in result
        assert len(result["Z"]) == 3
        assert result["Z"] == (42, 42, 42)

    @pytest.mark.parametrize("is_async", [False, True])
    def test_memory_dataset_input(self, is_async, fan_out_fan_in):
        pipeline = Pipeline([fan_out_fan_in])
        catalog = DataCatalog({"A": MemoryDataSet("42")})
        result = ParallelRunner(is_async=is_async).run(pipeline, catalog)
        assert "Z" in result
        assert len(result["Z"]) == 3
        assert result["Z"] == ("42", "42", "42")


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Due to bug in parallel runner"
)
class TestMaxWorkers:
    @pytest.mark.parametrize("is_async", [False, True])
    @pytest.mark.parametrize(
        "cpu_cores, user_specified_number, expected_number",
        [
            # The pipeline only needs 3 processes, no need for more
            (4, 6, 3),
            (4, None, 3),
            # We need 3 processes, but only 2 CPU cores available
            (2, None, 2),
            # Even though we have 1 CPU core, allow user to use more.
            (1, 2, 2),
        ],
    )
    def test_specified_max_workers_bellow_cpu_cores_count(
        self,
        is_async,
        mocker,
        fan_out_fan_in,
        catalog,
        cpu_cores,
        user_specified_number,
        expected_number,
    ):  # pylint: disable=too-many-arguments
        """
        The system has 2 cores, but we initialize the runner with max_workers=4.
        `fan_out_fan_in` pipeline needs 3 processes.
        A pool with 3 workers should be used.
        """
        mocker.patch("os.cpu_count", return_value=cpu_cores)

        executor_cls_mock = mocker.patch(
            "kedro.runner.parallel_runner.ProcessPoolExecutor",
            wraps=ProcessPoolExecutor,
        )

        catalog.add_feed_dict(dict(A=42))
        result = ParallelRunner(
            max_workers=user_specified_number, is_async=is_async
        ).run(fan_out_fan_in, catalog)
        assert result == {"Z": (42, 42, 42)}

        executor_cls_mock.assert_called_once_with(max_workers=expected_number)

    def test_max_worker_windows(self, mocker):
        """The ProcessPoolExecutor on Python 3.7+
        has a quirk with the max worker number on Windows
        and requires it to be <=61
        """
        mocker.patch("os.cpu_count", return_value=100)
        mocker.patch("sys.platform", "win32")

        parallel_runner = ParallelRunner()
        assert parallel_runner._max_workers == _MAX_WINDOWS_WORKERS


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Due to bug in parallel runner"
)
@pytest.mark.parametrize("is_async", [False, True])
class TestInvalidParallelRunner:
    def test_task_validation(self, is_async, fan_out_fan_in, catalog):
        """ParallelRunner cannot serialize the lambda function."""
        catalog.add_feed_dict(dict(A=42))
        pipeline = Pipeline([fan_out_fan_in, node(lambda x: x, "Z", "X")])
        with pytest.raises(AttributeError):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_task_exception(self, is_async, fan_out_fan_in, catalog):
        catalog.add_feed_dict(feed_dict=dict(A=42))
        pipeline = Pipeline([fan_out_fan_in, node(exception_fn, "Z", "X")])
        with pytest.raises(Exception, match="test exception"):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_memory_dataset_output(self, is_async, fan_out_fan_in):
        """ParallelRunner does not support output to externally
        created MemoryDataSets.
        """
        pipeline = Pipeline([fan_out_fan_in])
        catalog = DataCatalog({"C": MemoryDataSet()}, dict(A=42))
        with pytest.raises(AttributeError, match="['C']"):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_node_returning_none(self, is_async):
        pipeline = Pipeline([node(identity, "A", "B"), node(return_none, "B", "C")])
        catalog = DataCatalog({"A": MemoryDataSet("42")})
        pattern = "Saving 'None' to a 'DataSet' is not allowed"
        with pytest.raises(DataSetError, match=pattern):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_data_set_not_serializable(self, is_async, fan_out_fan_in):
        """Data set A cannot be serializable because _load and _save are not
        defined in global scope.
        """

        def _load():
            return 0  # pragma: no cover

        def _save(arg):
            assert arg == 0  # pragma: no cover

        # Data set A cannot be serialized
        catalog = DataCatalog({"A": LambdaDataSet(load=_load, save=_save)})

        pipeline = Pipeline([fan_out_fan_in])
        with pytest.raises(AttributeError, match="['A']"):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_memory_dataset_not_serializable(self, is_async, catalog):
        """Memory dataset cannot be serializable because of data it stores."""
        data = return_not_serializable(None)
        pipeline = Pipeline([node(return_not_serializable, "A", "B")])
        catalog.add_feed_dict(feed_dict=dict(A=42))
        pattern = (
            rf"{str(data.__class__)} cannot be serialized. ParallelRunner implicit "
            rf"memory datasets can only be used with serializable data"
        )

        with pytest.raises(DataSetError, match=pattern):
            ParallelRunner(is_async=is_async).run(pipeline, catalog)

    def test_unable_to_schedule_all_nodes(
        self, mocker, is_async, fan_out_fan_in, catalog
    ):
        """Test the error raised when `futures` variable is empty,
        but `todo_nodes` is not (can barely happen in real life).
        """
        catalog.add_feed_dict(dict(A=42))
        runner = ParallelRunner(is_async=is_async)

        real_node_deps = fan_out_fan_in.node_dependencies
        # construct deliberately unresolvable dependencies for all
        # pipeline nodes, so that none can be run
        fake_node_deps = {k: {"you_shall_not_pass"} for k in real_node_deps}
        # property mock requires patching a class, not an instance
        mocker.patch(
            "kedro.pipeline.Pipeline.node_dependencies",
            new_callable=mocker.PropertyMock,
            return_value=fake_node_deps,
        )

        pattern = "Unable to schedule new tasks although some nodes have not been run"
        with pytest.raises(RuntimeError, match=pattern):
            runner.run(fan_out_fan_in, catalog)


class LoggingDataSet(AbstractDataSet):
    def __init__(self, log, name, value=None):
        self.log = log
        self.name = name
        self.value = value

    def _load(self) -> Any:
        self.log.append(("load", self.name))
        return self.value

    def _save(self, data: Any) -> None:
        self.value = data

    def _release(self) -> None:
        self.log.append(("release", self.name))
        self.value = None

    def _describe(self) -> Dict[str, Any]:
        return {}


if not sys.platform.startswith("win"):
    ParallelRunnerManager.register(  # pylint: disable=no-member
        "LoggingDataSet", LoggingDataSet
    )


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Due to bug in parallel runner"
)
@pytest.mark.parametrize("is_async", [False, True])
class TestParallelRunnerRelease:
    def test_dont_release_inputs_and_outputs(self, is_async):
        runner = ParallelRunner(is_async=is_async)
        log = runner._manager.list()

        pipeline = Pipeline(
            [node(identity, "in", "middle"), node(identity, "middle", "out")]
        )
        # pylint: disable=no-member
        catalog = DataCatalog(
            {
                "in": runner._manager.LoggingDataSet(log, "in", "stuff"),
                "middle": runner._manager.LoggingDataSet(log, "middle"),
                "out": runner._manager.LoggingDataSet(log, "out"),
            }
        )
        ParallelRunner().run(pipeline, catalog)

        # we don't want to see release in or out in here
        assert list(log) == [("load", "in"), ("load", "middle"), ("release", "middle")]

    def test_release_at_earliest_opportunity(self, is_async):
        runner = ParallelRunner(is_async=is_async)
        log = runner._manager.list()

        pipeline = Pipeline(
            [
                node(source, None, "first"),
                node(identity, "first", "second"),
                node(sink, "second", None),
            ]
        )
        # pylint: disable=no-member
        catalog = DataCatalog(
            {
                "first": runner._manager.LoggingDataSet(log, "first"),
                "second": runner._manager.LoggingDataSet(log, "second"),
            }
        )
        runner.run(pipeline, catalog)

        # we want to see "release first" before "load second"
        assert list(log) == [
            ("load", "first"),
            ("release", "first"),
            ("load", "second"),
            ("release", "second"),
        ]

    def test_count_multiple_loads(self, is_async):
        runner = ParallelRunner(is_async=is_async)
        log = runner._manager.list()

        pipeline = Pipeline(
            [
                node(source, None, "dataset"),
                node(sink, "dataset", None, name="bob"),
                node(sink, "dataset", None, name="fred"),
            ]
        )
        # pylint: disable=no-member
        catalog = DataCatalog(
            {"dataset": runner._manager.LoggingDataSet(log, "dataset")}
        )
        runner.run(pipeline, catalog)

        # we want to the release after both the loads
        assert list(log) == [
            ("load", "dataset"),
            ("load", "dataset"),
            ("release", "dataset"),
        ]

    def test_release_transcoded(self, is_async):
        runner = ParallelRunner(is_async=is_async)
        log = runner._manager.list()

        pipeline = Pipeline(
            [node(source, None, "ds@save"), node(sink, "ds@load", None)]
        )
        catalog = DataCatalog(
            {
                "ds@save": LoggingDataSet(log, "save"),
                "ds@load": LoggingDataSet(log, "load"),
            }
        )

        ParallelRunner().run(pipeline, catalog)

        # we want to see both datasets being released
        assert list(log) == [("release", "save"), ("load", "load"), ("release", "load")]


@pytest.mark.parametrize("is_async", [False, True])
class TestRunNodeSynchronisationHelper:
    """Test class for _run_node_synchronization helper. It is tested manually
    in isolation since it's called in the subprocess, which ParallelRunner
    patches have no access to.
    """

    @pytest.fixture(autouse=True)
    def mock_logging(self, mocker):
        return mocker.patch("logging.config.dictConfig")

    @pytest.fixture
    def mock_run_node(self, mocker):
        return mocker.patch("kedro.runner.parallel_runner.run_node")

    @pytest.fixture
    def mock_configure_project(self, mocker):
        return mocker.patch("kedro.framework.project.configure_project")

    @pytest.mark.parametrize("conf_logging", [{"fake_logging_config": True}, {}])
    def test_package_name_and_logging_provided(
        self,
        mock_logging,
        mock_run_node,
        mock_configure_project,
        is_async,
        conf_logging,
        mocker,
    ):
        mocker.patch("multiprocessing.get_start_method", return_value="spawn")
        node_ = mocker.sentinel.node
        catalog = mocker.sentinel.catalog
        session_id = "fake_session_id"
        package_name = mocker.sentinel.package_name

        _run_node_synchronization(
            node_,
            catalog,
            is_async,
            session_id,
            package_name=package_name,
            conf_logging=conf_logging,
        )
        mock_run_node.assert_called_once()
        mock_logging.assert_called_once_with(conf_logging)
        mock_configure_project.assert_called_once_with(package_name)

    def test_package_name_provided(
        self,
        mock_logging,
        mock_run_node,
        mock_configure_project,
        is_async,
        mocker,
    ):
        mocker.patch("multiprocessing.get_start_method", return_value="spawn")
        node_ = mocker.sentinel.node
        catalog = mocker.sentinel.catalog
        session_id = "fake_session_id"
        package_name = mocker.sentinel.package_name

        _run_node_synchronization(
            node_, catalog, is_async, session_id, package_name=package_name
        )
        mock_run_node.assert_called_once()
        # No project-side logging.yml has been provided, so logging should not be re-configured.
        mock_logging.assert_not_called()
        mock_configure_project.assert_called_once_with(package_name)

    def test_package_name_not_provided(
        self, mock_logging, mock_run_node, is_async, mocker
    ):
        mocker.patch("multiprocessing.get_start_method", return_value="fork")
        node_ = mocker.sentinel.node
        catalog = mocker.sentinel.catalog
        session_id = "fake_session_id"
        package_name = mocker.sentinel.package_name

        _run_node_synchronization(
            node_, catalog, is_async, session_id, package_name=package_name
        )
        mock_run_node.assert_called_once()
        mock_logging.assert_not_called()
