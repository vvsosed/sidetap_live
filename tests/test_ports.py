"""Every fake must satisfy the Protocol it stands in for.

runtime_checkable only checks method names, not signatures, so this is a
shallow guard. It still catches the common failure: a fake that drifts after
its Protocol gains a method.
"""

from sidetap_live.ports import (
    AudioSink,
    Clock,
    GraphSource,
    Linker,
    LoopbackFactory,
    ManagedProcess,
    ProcessLauncher,
    Unlinker,
    VolumeControl,
    WritableProcess,
)
from tests.conftest import (
    FakeAudioSink,
    FakeClock,
    FakeGraphSource,
    FakeLauncher,
    FakeLinker,
    FakeLoopbackFactory,
    FakeProcess,
    FakeVolumeControl,
    FakeWritableProcess,
)


def test_fakes_satisfy_their_protocols(idle_graph):
    assert isinstance(FakeGraphSource(idle_graph), GraphSource)
    assert isinstance(FakeLauncher(), ProcessLauncher)
    assert isinstance(FakeLinker(), Linker)
    assert isinstance(FakeLinker(), Unlinker)
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeVolumeControl(), VolumeControl)
    assert isinstance(FakeLoopbackFactory(), LoopbackFactory)
    assert isinstance(FakeAudioSink(), AudioSink)


def test_fake_processes_satisfy_their_protocols():
    import io

    assert isinstance(FakeProcess(io.BytesIO(b"")), ManagedProcess)
    assert isinstance(FakeWritableProcess(), WritableProcess)


from sidetap_live.ports import InterpreterSession, SessionFactory
from tests.conftest import FakeSession, FakeSessionFactory


def test_session_fakes_satisfy_their_protocols():
    assert isinstance(FakeSession(), InterpreterSession)
    assert isinstance(FakeSessionFactory(), SessionFactory)


def test_close_ends_the_event_iterator():
    session = FakeSession()
    session.close()
    assert list(session.events()) == []
