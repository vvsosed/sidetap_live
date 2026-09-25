import dataclasses
import json
import logging
import os
import threading

import pytest

from sidetap_live.graph import PLAYBACK_STREAM, PwLink, PwNode, PwPort
from sidetap_live.ports import LinkResult
from sidetap_live.routing import (
    DUCK_NODE,
    VIRTMIC_CAPTURE_DESCRIPTION,
    VIRTMIC_CONFIG,
    VIRTMIC_DESCRIPTION,
    VIRTMIC_SINK,
    VIRTMIC_SOURCE,
    Journal,
    LinkRef,
    Router,
    call_sink,
    duck_loopback_spec,
    resolve,
    sink_links,
)
from sidetap_live.tap import GRAPH_ERROR_WARN_AFTER
from tests.conftest import (
    FakeGraphSource,
    FakeLinker,
    FakeLoopbackFactory,
    LiveLinks,
    load_graph,
    playing_on,
)


def test_the_duck_loopback_presents_a_sink_we_can_route_into():
    spec = duck_loopback_spec(target_sink="alsa_output.pci-0000_00_1f.3.analog-stereo")
    capture = dict(spec.capture_props)
    playback = dict(spec.playback_props)
    assert capture["node.name"] == DUCK_NODE
    assert capture["media.class"] == "Audio/Sink"
    # The playback side must land on the real speakers, or the user hears
    # nothing at all once the app is re-routed.
    assert playback["target.object"] == "alsa_output.pci-0000_00_1f.3.analog-stereo"


def test_link_refs_round_trip_through_json():
    ref = LinkRef(src_serial=10, src_port="output_FL", dst_serial=20, dst_port="playback_FL")
    assert LinkRef.from_dict(json.loads(json.dumps(ref.to_dict()))) == ref


def test_journal_round_trips_through_a_file(tmp_path):
    path = tmp_path / "journal.json"
    journal = Journal(
        broken=(LinkRef(1, "a", 2, "b"),),
        made=(LinkRef(1, "a", 3, "c"),),
    )
    journal.save(path)
    assert Journal.load(path) == journal


def test_loading_a_missing_journal_gives_an_empty_one(tmp_path):
    assert Journal.load(tmp_path / "nope.json") == Journal()


def test_loading_a_corrupt_journal_gives_an_empty_one(tmp_path):
    # A half-written file from a kill -9 must not stop the next run.
    path = tmp_path / "journal.json"
    path.write_text("{ not json")
    assert Journal.load(path) == Journal()


def test_resolve_finds_port_ids_by_serial_and_name(routing_graph):
    """A real link: an output port on a stream, an input port on a sink.

    This used to name the same out port as BOTH ends, so the dst lookup -
    ports_of(node, "in") - never matched and the assertion only held because
    resolve() fell back to searching every port of the node regardless of
    direction. It was passing on the strength of the very behaviour
    test_resolve_refuses_a_port_of_the_wrong_direction now forbids.
    """
    stream = routing_graph.by_class("Stream/Output/Audio")[0]
    out_port = routing_graph.ports_of(stream.id, "out")[0]
    sink = routing_graph.node_by_name(routing_graph.default_sink)
    in_port = routing_graph.ports_of(sink.id, "in")[0]

    ref = LinkRef(
        src_serial=stream.serial,
        src_port=out_port.name,
        dst_serial=sink.serial,
        dst_port=in_port.name,
    )
    assert resolve(routing_graph, ref) == (out_port.id, in_port.id)


def test_resolve_refuses_a_port_of_the_wrong_direction(routing_graph):
    """Failing loud beats handing pw-link a backwards port.

    resolve() retried a missed lookup across every port of the node ignoring
    direction. Nothing explained when that was meant to fire and nothing
    tested it; what it would actually do is quietly produce a link nobody
    asked for, journalled as though it were the intended one.
    """
    sink = routing_graph.node_by_name(routing_graph.default_sink)
    in_port = routing_graph.ports_of(sink.id, "in")[0]

    # Name the sink's INPUT port as the SOURCE end of the link.
    ref = LinkRef(
        src_serial=sink.serial,
        src_port=in_port.name,
        dst_serial=sink.serial,
        dst_port=in_port.name,
    )
    assert resolve(routing_graph, ref) is None


def test_resolve_returns_none_when_the_node_is_gone(routing_graph):
    # Serials are never recycled, so a missing one means the stream ended.
    ref = LinkRef(src_serial=999999, src_port="x", dst_serial=1, dst_port="y")
    assert resolve(routing_graph, ref) is None


def test_engage_journals_before_it_touches_the_graph(tmp_path, routing_graph):
    """The journal must be durable before the unlink, not after.

    A crash in between is exactly the case it exists for.
    """
    path = tmp_path / "journal.json"
    writes = []

    class WatchingLinker(FakeLinker):
        def unlink(self, src, dst):
            writes.append(("unlink", path.exists()))
            return super().unlink(src, dst)

    linker = WatchingLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom")

    assert writes, "no unlink happened at all"
    assert all(existed for _, existed in writes)


def test_engage_creates_the_duck_loopback(tmp_path, routing_graph):
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    assert len(loopbacks.specs) == 1


def test_restore_terminates_the_duck_loopback(tmp_path, routing_graph):
    """A leaked pw-loopback is a leaked duck node - restore() must kill it.

    Not covered by any other test here: none of them inspect the loopback
    process restore() is handed, only the links it produces.
    """
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    router.restore()
    assert loopbacks.processes[0].terminated


def test_restore_relinks_what_was_broken_and_breaks_what_was_made(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    made = len(linker.links)
    broken = len(linker.unlinks)

    linker.links.clear()
    linker.unlinks.clear()
    router.restore()

    assert len(linker.links) == broken
    assert len(linker.unlinks) == made


def test_restore_clears_the_journal(tmp_path, routing_graph):
    path = tmp_path / "j.json"
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom")
    router.restore()
    assert Journal.load(path) == Journal()


def test_restore_is_idempotent(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    router.restore()
    linker.links.clear()
    router.restore()
    assert linker.links == []


def test_repair_replays_a_journal_from_a_dead_session(tmp_path, routing_graph):
    """kill -9 leaves the graph broken and the journal behind."""
    path = tmp_path / "j.json"
    # A real link, as the journal would actually hold it: a stream's output
    # port into the default sink's input port. Naming one out port as both
    # ends, as this used to, only resolved because resolve() searched ports
    # ignoring direction.
    stream = routing_graph.by_class("Stream/Output/Audio")[0]
    port = routing_graph.ports_of(stream.id, "out")[0]
    sink = routing_graph.node_by_name(routing_graph.default_sink)
    in_port = routing_graph.ports_of(sink.id, "in")[0]
    Journal(
        broken=(LinkRef(stream.serial, port.name, sink.serial, in_port.name),), made=()
    ).save(path)

    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    assert router.repair() is True
    assert linker.links == [(port.id, in_port.id)]
    assert Journal.load(path) == Journal()


def test_repair_with_no_journal_reports_nothing_to_do(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    assert router.repair() is False


def test_a_stale_journal_entry_is_skipped_not_fatal(tmp_path, routing_graph):
    # The application exited before repair ran; its serial is gone forever.
    path = tmp_path / "j.json"
    Journal(broken=(LinkRef(999999, "a", 999998, "b"),), made=()).save(path)
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    assert router.repair() is True
    assert linker.links == []
    assert Journal.load(path) == Journal()


def test_engage_pairs_channels_rather_than_crossing_them(tmp_path, routing_graph):
    """FL to FL, FR to FR - the way WirePlumber linked them in the first place.

    Journalling a cross-product would make restore CREATE links that never
    existed, leaving the graph worse than sidetap found it.
    """
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    journal = Journal.load(tmp_path / "j.json")
    assert [(r.src_port, r.dst_port) for r in journal.broken] == [
        ("output_FL", "playback_FL"),
        ("output_FR", "playback_FR"),
    ]
    assert [(r.src_port, r.dst_port) for r in journal.made] == [
        ("output_FL", "playback_FL"),
        ("output_FR", "playback_FR"),
    ]


def test_poll_routes_a_stream_that_appeared_after_engage(tmp_path, routing_graph):
    """A restarted stream is otherwise autoconnected to the speakers.

    Unducked and unjournalled, so the original plays over the translation for
    the rest of the call and restore() cannot put it back.
    """
    from dataclasses import replace

    from sidetap_live.graph import PLAYBACK_STREAM

    empty = replace(
        routing_graph,
        nodes=tuple(n for n in routing_graph.nodes if n.media_class != PLAYBACK_STREAM),
    )
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(empty, routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    assert linker.links == [], "nothing was playing yet"

    assert router.poll_once() == 1
    assert linker.links, "the stream that appeared later was never routed"
    assert Journal.load(tmp_path / "j.json").made


def test_poll_does_not_reroute_the_same_stream(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    before = len(linker.links)
    assert router.poll_once() == 0
    assert len(linker.links) == before


def test_poll_before_engage_does_nothing(tmp_path, routing_graph):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    assert router.poll_once() == 0
    assert linker.links == []


def test_engage_records_both_the_ducks_serial_and_its_id(tmp_path, routing_graph):
    """They are different numbers and are used for different things.

    The journal needs the serial, because ids are recycled over time. wpctl
    needs the id, because that is what it resolves against. Feeding a serial
    to wpctl makes the duck silently never close, and no fake-based test can
    see that - FakeVolumeControl records whatever int it is handed.
    """
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    duck = routing_graph.node_by_name(DUCK_NODE)
    assert router.duck_serial == duck.serial
    assert router.duck_id == duck.id
    assert duck.serial != duck.id, "fixture must keep them distinct to be meaningful"


def test_ports_are_ordered_by_name_so_index_pairing_is_stable(routing_graph):
    """engage() pairs ports by index, so ports_of()'s ordering is load-bearing.

    graph.py sorts by port name. The fixtures happen to list FL before FR, so
    a test that only checked the fixture's own order would pass whether or not
    sorting happened at all - this asserts the sort explicitly, because a
    silent reordering would cross channels while re-routing live call audio.
    """
    stream = next(
        n for n in routing_graph.by_class("Stream/Output/Audio") if n.matches("zoom")
    )
    names = [p.name for p in routing_graph.ports_of(stream.id, "out")]
    assert names == sorted(names)
    assert names == ["output_FL", "output_FR"]


def test_engage_leaves_sidetaps_own_sinks_alone(tmp_path, routing_graph):
    """Only the links the call actually has are unlinked, never every sink."""
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    journal = Journal.load(tmp_path / "j.json")
    # 1400 is sidetap_tts_sink: touching it would cut the virtual mic.
    assert all(r.dst_serial != 1400 for r in journal.broken)
    assert all(r.dst_serial == 1001 for r in journal.broken)


def test_the_virtmic_config_declares_both_halves():
    assert VIRTMIC_SINK in VIRTMIC_CONFIG
    assert VIRTMIC_SOURCE in VIRTMIC_CONFIG
    assert "Audio/Sink" in VIRTMIC_CONFIG
    assert "Audio/Source" in VIRTMIC_CONFIG
    assert "libpipewire-module-loopback" in VIRTMIC_CONFIG


# --- Quality review follow-ups -------------------------------------------


def test_a_failed_route_is_not_marked_routed_and_is_retried(tmp_path, routing_graph):
    """A FAILED apply must not be recorded as routed.

    tap.py's AppTap deliberately leaves a failed pair unrecorded "so it is
    retried" - Router must behave the same way. Recording it anyway would
    mean: if the unlink from the speakers succeeds but the link into the
    duck fails, the stream ends up connected to nothing at all, and
    poll_once() would never try again because the serial already looks done.
    """
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    attempts_after_engage = len(linker.links) + len(linker.unlinks)
    assert attempts_after_engage > 0, "engage() should still have tried"

    # If the stream had been (wrongly) marked routed, this call would make no
    # further linker calls at all.
    router.poll_once()
    attempts_after_poll = len(linker.links) + len(linker.unlinks)
    assert attempts_after_poll > attempts_after_engage, "a failed route was never retried"


def test_a_failed_route_succeeds_once_the_transient_failure_clears(tmp_path, routing_graph):
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    linker.result = LinkResult.LINKED
    assert router.poll_once() == 1


def test_restore_keeps_the_journal_when_a_link_fails(tmp_path, routing_graph):
    """A journal that survives one failed restore is recoverable; one that

    gets erased anyway is not. A transient pw-link timeout during a normal
    exit must not both fail to restore the graph AND destroy the only record
    that could repair it at next startup.
    """
    path = tmp_path / "j.json"
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom")
    before = Journal.load(path)
    assert not before.is_empty()

    linker.result = LinkResult.FAILED
    router.restore()

    assert Journal.load(path) == before, "a failed restore must not erase the journal"


def test_restore_logs_an_error_when_it_cannot_fully_restore(tmp_path, routing_graph, caplog):
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    linker.result = LinkResult.FAILED
    with caplog.at_level(logging.ERROR):
        router.restore()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_journal_save_is_atomic_a_failed_write_does_not_corrupt_the_previous_file(
    tmp_path, monkeypatch
):
    """_route() does a load-modify-save of the ACCUMULATED journal on every

    call, so a torn write on the second or later call would destroy the
    record of mutations that are already applied and live, not just the one
    being added. save() must write elsewhere and swap the file in atomically.
    """
    path = tmp_path / "journal.json"
    original = Journal(broken=(LinkRef(1, "a", 2, "b"),))
    original.save(path)
    original_bytes = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    newer = Journal(broken=(LinkRef(9, "x", 9, "y"),))
    with pytest.raises(OSError):
        newer.save(path)

    assert path.read_bytes() == original_bytes, "a failed save corrupted the live journal"


def test_loading_a_corrupt_journal_logs_an_error(tmp_path, caplog):
    # Unlike a merely missing journal, a file that exists but cannot be
    # parsed can only mean a write was interrupted - possibly the only record
    # of a link that is still live. That must not be silent.
    path = tmp_path / "journal.json"
    path.write_text("{ not json")
    with caplog.at_level(logging.ERROR):
        Journal.load(path)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_loading_a_missing_journal_does_not_log_an_error(tmp_path, caplog):
    # The common case - no session has ever journalled here - must stay
    # quiet, or every ordinary startup would print a scary error.
    with caplog.at_level(logging.WARNING):
        Journal.load(tmp_path / "nope.json")
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_route_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.engage(app_pattern="zoom")
    assert held_during_read == [False], "engage() must hold the lock while reading the graph"


def test_restore_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.restore()
    assert held_during_read == [False], "restore() must hold the lock while reading the graph"


def test_repair_holds_the_lock_while_reading_the_graph(tmp_path, routing_graph):
    router = Router(
        graph=None,
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    held_during_read = []

    class SpyGraph:
        def snapshot(self):
            acquired = router._lock.acquire(blocking=False)
            held_during_read.append(acquired)
            if acquired:
                router._lock.release()
            return routing_graph

    router._graph = SpyGraph()
    router.repair()
    assert held_during_read == [False], "repair() must hold the lock while reading the graph"


def test_engage_twice_raises_instead_of_leaking_a_second_duck(tmp_path, routing_graph):
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    with pytest.raises(RuntimeError):
        router.engage(app_pattern="zoom")


def test_repair_warns_about_an_orphaned_duck_node(tmp_path, routing_graph, caplog):
    """A kill -9 leaves pw-loopback running under start_new_session=True.

    repair() has no PID to kill it with - the journal records links, not
    processes - so the best it can do is tell the user how to find it by
    hand.
    """
    router = Router(
        graph=FakeGraphSource(routing_graph),  # already contains sidetap_duck
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "absent.json",
    )
    with caplog.at_level(logging.WARNING):
        router.repair()
    assert any("pkill" in r.message for r in caplog.records)


def test_the_virtmic_config_distinguishes_the_two_nodes():
    """Both nodes showing the same description in a volume UI invites

    picking the wrong one - the symptom would be the remote party hearing
    nothing, with no error anywhere to explain why.
    """
    assert VIRTMIC_CAPTURE_DESCRIPTION != VIRTMIC_DESCRIPTION
    _, after_capture = VIRTMIC_CONFIG.split("capture.props")
    capture_section, playback_section = after_capture.split("playback.props")
    assert VIRTMIC_CAPTURE_DESCRIPTION in capture_section
    assert VIRTMIC_DESCRIPTION not in capture_section
    assert VIRTMIC_DESCRIPTION in playback_section


def test_restore_terminates_the_duck_even_when_no_stream_was_ever_routed(
    tmp_path, idle_graph
):
    """Starting sidetap before the call and quitting before it begins.

    Nothing matches the app pattern, so the journal stays empty for the whole
    session. restore() used to return early on that and skip the loopback it
    created in engage(), orphaning a pw-loopback whose PID no later run can
    recover - and the next engage() then adds a SECOND node called
    sidetap_duck, after which node_by_name picks one of them arbitrarily.
    """
    loopbacks = FakeLoopbackFactory()
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(idle_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=loopbacks,
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="nothing-is-playing")
    assert not (tmp_path / "j.json").exists() or Journal.load(
        tmp_path / "j.json"
    ).is_empty()
    router.restore()
    assert loopbacks.processes[0].terminated, "the duck outlived the session"


def test_a_persistently_failing_stream_does_not_grow_the_journal(
    tmp_path, routing_graph
):
    """The retry is deliberate; re-recording the same refs each time is not.

    A failing stream is kept out of _routed so every poll retries it. Appending
    its refs unconditionally meant a full JSON rewrite per poll, under the lock
    shutdown needs to restore the graph, and every duplicate replayed again on
    the way out.
    """
    linker = FakeLinker(result=LinkResult.FAILED)
    router = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    after_engage = Journal.load(tmp_path / "j.json")
    for _ in range(5):
        router.poll_once()
    after_polls = Journal.load(tmp_path / "j.json")

    assert after_engage.broken, "the fixture must produce refs or this proves nothing"
    assert len(after_polls.broken) == len(after_engage.broken)
    assert len(after_polls.made) == len(after_engage.made)


def test_a_failed_journal_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """The temp file is the whole mechanism; a stray one accumulates silently."""
    path = tmp_path / "j.json"
    Journal(broken=(LinkRef(1, "a", 2, "b"),)).save(path)

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr("sidetap_live.routing.os.replace", boom)
    with pytest.raises(OSError):
        Journal(broken=(LinkRef(3, "c", 4, "d"),)).save(path)

    assert Journal.load(path).broken == (LinkRef(1, "a", 2, "b"),)
    assert list(tmp_path.glob("*.tmp")) == []


def test_has_routed_is_false_until_a_stream_is_actually_rewired(
    tmp_path, idle_graph, routing_graph
):
    """It arms the no-audio alarm, so it must mean "the app is playing"."""
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(idle_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    assert router.has_routed is False, "nothing in the idle graph is playing"

    linker2 = FakeLinker()
    playing = Router(
        graph=FakeGraphSource(routing_graph),
        linker=linker2,
        unlinker=linker2,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "k.json",
    )
    playing.engage(app_pattern="zoom")
    assert playing.has_routed is True


def _without_duck_ports(graph):
    """The same graph one registry beat earlier: duck Node, no Ports yet."""
    duck = graph.node_by_name(DUCK_NODE)
    return dataclasses.replace(
        graph, ports=tuple(p for p in graph.ports if p.node_id != duck.id)
    )


def _without_duck(graph):
    """Before pw-loopback has registered anything - what engage() really sees."""
    duck = graph.node_by_name(DUCK_NODE)
    return dataclasses.replace(
        graph,
        nodes=tuple(n for n in graph.nodes if n.id != duck.id),
        ports=tuple(p for p in graph.ports if p.node_id != duck.id),
    )


def test_a_duck_with_no_ports_yet_does_not_unlink_the_call_from_the_speakers(
    tmp_path, routing_graph
):
    """PipeWire announces a Node before its Ports finish registering.

    engage() deliberately snapshots before spawning pw-loopback, so the first
    poll_once() can land in the window where the duck node exists but has no
    input ports. The unlink from the speakers and the link into the duck are
    decided independently, so that window unlinked the call from the speakers
    and linked it to nothing - and because both unlinks SUCCEEDED, the stream
    was marked routed and never retried. Silent call, nothing in the log.
    """
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(
            _without_duck(routing_graph),        # engage(): pre-spawn
            _without_duck_ports(routing_graph),  # first poll: the race window
            routing_graph,                       # later poll: duck fully up
        ),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    router.poll_once()
    assert linker.unlinks == [], (
        "the call was unlinked from the speakers while the duck had no ports "
        "to link it into - the user now hears nothing"
    )

    # And the stream must not have been recorded as done: once the duck's
    # ports appear, the next poll has to complete the routing.
    router.poll_once()
    assert linker.unlinks, "the deferred stream was never routed once the duck was ready"
    assert linker.links, "the deferred stream was never linked into the duck"


def test_the_routing_watcher_warns_once_it_keeps_failing(tmp_path, routing_graph, caplog):
    """AppTap.run() escalates after GRAPH_ERROR_WARN_AFTER consecutive
    failures, because being blind to new streams for a whole meeting "must
    not be debug-only" - and routing.py's own header says the Router watcher
    exists for the same reason. It logged at DEBUG forever instead.

    A persistent failure here means the duck stops picking up new streams, so
    the remote party's original plays over every translation for the rest of
    the call, with nothing at default log level to say so.
    """

    class BrokenGraph:
        def snapshot(self):
            raise OSError("pw-dump: command not found")

    router = Router(
        graph=BrokenGraph(),
        linker=FakeLinker(),
        unlinker=FakeLinker(),
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router._app_pattern = "zoom"

    stop = threading.Event()

    class OneShot(threading.Event):
        """Lets run() poll exactly GRAPH_ERROR_WARN_AFTER times."""

        def __init__(self):
            super().__init__()
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits >= GRAPH_ERROR_WARN_AFTER:
                self.set()
            return super().wait(0)

    stop = OneShot()
    with caplog.at_level(logging.WARNING, logger="sidetap_live.routing"):
        router.run(stop, interval=0.0)

    assert any("repeatedly" in r.message for r in caplog.records), (
        "the routing watcher never escalated past DEBUG"
    )


def test_the_journal_is_flushed_to_disk_before_the_rename(tmp_path, monkeypatch):
    """The journal's whole purpose is surviving a crash mid-rewire.

    tmp + os.replace() already protected against a torn file, but nothing was
    fsynced, so the bar it actually met was "kill -9 with the OS still up"
    rather than the power loss its own docstring implies. A journal that
    reverts leaves the graph rewired with nothing on disk to repair from.
    """
    import sidetap_live.routing as routing_module

    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        routing_module.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1]
    )

    path = tmp_path / "state" / "j.json"
    Journal(broken=(LinkRef(1, "a", 2, "b"),), made=()).save(path)

    assert len(synced) >= 2, (
        "expected the temp file and its directory to be fsynced, "
        f"saw {len(synced)}"
    )
    assert Journal.load(path).broken[0].src_port == "a"


# --- Routing moves the links that exist, on the device the call uses -----

HEADSET = "alsa_output.usb-headset"


def test_restore_recreates_the_links_that_existed_and_no_others(tmp_path, routing_graph):
    """Zoom on a USB headset while the system default is the speakers.

    Routing assumed every stream played to the default sink, so it journalled
    zoom->speakers links that never existed and restore() created them: after
    the program quit, the call played from the headset AND the speakers.
    """
    live = LiveLinks(playing_on(routing_graph, HEADSET))
    before = set(live.pairs)
    router = Router(
        graph=live,
        linker=live,
        unlinker=live,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")
    router.restore()

    assert live.pairs == before, (
        f"restore() created {sorted(live.pairs - before)} and lost "
        f"{sorted(before - live.pairs)}"
    )


def _zoom_out_ports(graph):
    stream = graph.find("zoom", PLAYBACK_STREAM)
    return {p.id for p in graph.ports_of(stream.id, "out")}


def _in_ports(graph, name):
    return {p.id for p in graph.ports_of(graph.node_by_name(name).id, "in")}


def _live_router(tmp_path, live, loopbacks=None):
    return Router(
        graph=live,
        linker=live,
        unlinker=live,
        loopbacks=loopbacks or FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )


@pytest.mark.parametrize("sink_name", ["alsa_output.default", HEADSET])
def test_the_call_moves_into_the_duck_and_back_exactly(
    tmp_path, routing_graph, sink_name, caplog
):
    """The default sink and any other device take the same path."""
    live = LiveLinks(playing_on(routing_graph, sink_name))
    before = set(live.pairs)
    zoom = _zoom_out_ports(routing_graph)
    router = _live_router(tmp_path, live)

    with caplog.at_level(logging.WARNING, logger="sidetap_live.routing"):
        router.engage(app_pattern="zoom")

    assert router.has_routed
    targets = {dst for src, dst in live.pairs if src in zoom}
    assert targets == _in_ports(routing_graph, DUCK_NODE), (
        "zoom should play only into the duck while engaged"
    )
    # An unlink of a link that does not exist fails, and was retried and
    # warned about every poll for the rest of the call.
    assert not caplog.records, [r.getMessage() for r in caplog.records]

    router.restore()
    assert live.pairs == before


def test_the_duck_plays_on_the_device_the_call_plays_on(tmp_path, routing_graph):
    """Otherwise the original AND the translation play on the speakers,
    which leak into the microphone, while the headset keeps the original."""
    loopbacks = FakeLoopbackFactory()
    router = _live_router(tmp_path, LiveLinks(playing_on(routing_graph, HEADSET)), loopbacks)

    router.engage(app_pattern="zoom")

    assert dict(loopbacks.specs[0].playback_props)["target.object"] == HEADSET
    assert router.target_sink.serial == routing_graph.node_by_name(HEADSET).serial


def test_the_duck_plays_on_the_default_sink_before_the_call_plays(tmp_path, routing_graph):
    loopbacks = FakeLoopbackFactory()
    router = _live_router(tmp_path, LiveLinks(playing_on(routing_graph, None)), loopbacks)

    router.engage(app_pattern="zoom")

    assert dict(loopbacks.specs[0].playback_props)["target.object"] == "alsa_output.default"
    assert router.target_sink.name == routing_graph.default_sink


def test_a_stream_on_another_device_than_the_duck_is_left_untouched(
    tmp_path, routing_graph, caplog
):
    """Moving it would take the call off the device the user listens on.

    The duck was set up on the speakers before the call started; Zoom then
    plays to the headset. Left alone, the original stays audible there, which
    is the safe failure; one ERROR says how to fix it, and fixing it mid-call
    gets the stream routed with no restart.
    """
    path = tmp_path / "j.json"
    on_headset = playing_on(routing_graph, HEADSET)
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(
            playing_on(routing_graph, None),  # engage(): nothing playing yet
            on_headset,
            on_headset,
            routing_graph,  # the user moved Zoom to the speakers
        ),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=path,
    )
    router.engage(app_pattern="zoom")

    with caplog.at_level(logging.ERROR, logger="sidetap_live.routing"):
        assert router.poll_once() == 0
        assert router.poll_once() == 0

    assert linker.links == [] and linker.unlinks == [], "a mismatched stream was touched"
    assert Journal.load(path).is_empty(), "a mismatched stream was journalled"
    assert router.has_routed is False
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected one ERROR, not one per poll: {errors}"
    assert "USB Headset" in errors[0] and "Speakers" in errors[0]

    assert router.poll_once() == 1, "a stream moved to the duck's device was not routed"


def test_a_stream_not_yet_linked_to_any_device_is_not_routed(tmp_path, routing_graph):
    """Nothing to journal means nothing to restore, and linking it into the
    duck would leave it in the duck AND wherever WirePlumber links it next."""
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(playing_on(routing_graph, None), routing_graph),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    assert linker.links == [] and linker.unlinks == []
    assert router.has_routed is False

    assert router.poll_once() == 1, "the stream was not routed once it was linked"


def test_a_stream_without_ports_is_not_marked_routed_with_another(tmp_path, routing_graph):
    """A second Zoom stream announced before its ports, in the same poll that
    routes the first, must still be routed once its ports appear."""
    second = PwNode(
        id=90,
        serial=1500,
        name="ZOOM VoiceEngine",
        description="ZOOM VoiceEngine",
        media_class=PLAYBACK_STREAM,
        app_name="zoom",
        app_binary="zoom",
    )
    announced = dataclasses.replace(routing_graph, nodes=routing_graph.nodes + (second,))
    with_ports = dataclasses.replace(
        announced,
        ports=announced.ports
        + (PwPort(901, 90, "output_FL", "out"), PwPort(902, 90, "output_FR", "out")),
        links=announced.links
        + (PwLink(951, 90, 901, 40, 401), PwLink(952, 90, 902, 40, 402)),
    )
    linker = FakeLinker()
    router = Router(
        graph=FakeGraphSource(announced, with_ports),
        linker=linker,
        unlinker=linker,
        loopbacks=FakeLoopbackFactory(),
        journal_path=tmp_path / "j.json",
    )
    router.engage(app_pattern="zoom")

    assert router.poll_once() == 1
    assert {901, 902} <= {src for src, _ in linker.links}


def test_the_tap_into_our_capture_node_is_never_moved(tmp_path, routing_graph):
    """The tap is how IN hears the call. It is a link from the same stream,
    but into a capture stream, not a device."""
    capture = PwNode(
        id=96,
        serial=1600,
        name="sidetap_live.remote.abcd1234",
        description="",
        media_class="Stream/Input/Audio",
    )
    tapped = dataclasses.replace(
        routing_graph,
        nodes=routing_graph.nodes + (capture,),
        ports=routing_graph.ports + (PwPort(961, 96, "input_MONO", "in"),),
        links=routing_graph.links
        + (PwLink(971, 55, 551, 96, 961), PwLink(972, 55, 552, 96, 961)),
    )
    live = LiveLinks(tapped)
    before = set(live.pairs)
    router = _live_router(tmp_path, live)

    router.engage(app_pattern="zoom")

    assert router.has_routed
    assert {(551, 961), (552, 961)} <= live.pairs, "the tap was unlinked"
    journal = Journal.load(tmp_path / "j.json")
    assert all(r.dst_serial != 1600 for r in journal.broken + journal.made)
    router.restore()
    assert live.pairs == before


def test_a_stream_unlinked_but_not_yet_in_the_duck_is_retried(tmp_path, routing_graph):
    """After the unlink lands and the link into the duck fails, the stream
    shows no device link at all. That must still read as ours to finish, not
    as a stream WirePlumber has yet to link, or the call stays silent."""
    live = LiveLinks(routing_graph, fail_links=2)
    before = set(live.pairs)
    zoom = _zoom_out_ports(routing_graph)
    router = _live_router(tmp_path, live)

    router.engage(app_pattern="zoom")
    assert not {dst for src, dst in live.pairs if src in zoom}, "zoom should be unlinked"
    assert router.has_routed is False

    assert router.poll_once() == 1
    assert {dst for src, dst in live.pairs if src in zoom} == _in_ports(
        routing_graph, DUCK_NODE
    )

    router.restore()
    assert live.pairs == before


def test_the_virtual_mic_is_never_taken_for_the_calls_device(tmp_path, routing_graph):
    """A messenger whose speaker is set to the virtual mic's sink.

    Following it there would send the IN translation, and the ducked original,
    to the remote party while the user hears nothing.
    """
    loopbacks = FakeLoopbackFactory()
    live = LiveLinks(playing_on(routing_graph, VIRTMIC_SINK))
    before = set(live.pairs)
    router = _live_router(tmp_path, live, loopbacks)

    router.engage(app_pattern="zoom")

    assert router.target_sink.name == routing_graph.default_sink
    assert dict(loopbacks.specs[0].playback_props)["target.object"] != VIRTMIC_SINK
    assert live.pairs == before, "a stream playing into the virtual mic was moved"


def test_sink_links_reads_a_real_pw_dump():
    """Hand-written fixtures share routing's assumptions; a real dump does not."""
    graph = load_graph("pw_dump_real.json")
    firefox = graph.find("firefox", PLAYBACK_STREAM)

    links = sink_links(graph, firefox)

    assert [(o.name, i.name, s.name) for o, i, s in links] == [
        ("output_FL", "playback_FL", graph.default_sink),
        ("output_FR", "playback_FR", graph.default_sink),
    ]
    assert call_sink(graph, "firefox").name == graph.default_sink
