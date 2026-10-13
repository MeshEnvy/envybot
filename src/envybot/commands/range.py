"""USB shootout: two repeaters ping the same neighbors and compare SNR."""

from __future__ import annotations

import argparse
import sys
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from envybot.commands.onboard import (
    CliError,
    RepeaterSerial,
    parse_radio,
    reply_failed,
    resolve_port_arg,
)
from envybot.flash_program import TARGETS

DEFAULT_ROUNDS = 3
DEFAULT_LISTEN_S = 12.0
DEFAULT_GAP_S = 30.0  # remote discover budget is 4 replies per 120s, shared

DISCOVER_RE_SRC = (
    r"discover\s+([0-9a-fA-F]{8})\s+rx=([+-]?\d+(?:\.\d+)?)\s+in=([+-]?\d+(?:\.\d+)?)"
    r"(?:\s+rssi=([+-]?\d+(?:\.\d+)?))?"
)


def _discover_re():
    import re

    return re.compile(DISCOVER_RE_SRC, re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    pubkey: str
    rx: float
    inbound: float
    rssi: float | None = None


@dataclass(frozen=True)
class RoundSnap:
    hits: dict[str, Hit]
    noise_floor: int | None
    battery_mv: int | None


@dataclass(frozen=True)
class BoardConfig:
    name: str
    port: str
    radio: str
    tx: str
    rxgain: str
    fem_rx: str
    fem_tx: str


@dataclass
class ScoreRow:
    label: str
    pubkey: str
    a_rx: list[float] = field(default_factory=list)
    b_rx: list[float] = field(default_factory=list)
    a_in: list[float] = field(default_factory=list)
    b_in: list[float] = field(default_factory=list)


def parse_discover_lines(text: str) -> list[Hit]:
    """Pull `discover <pubkey8> rx= in=` lines out of a USB capture."""
    pat = _discover_re()
    hits: list[Hit] = []
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in body.splitlines():
        match = pat.search(raw.strip())
        if not match:
            continue
        rssi = float(match.group(4)) if match.group(4) is not None else None
        hits.append(
            Hit(
                pubkey=match.group(1).lower(),
                rx=float(match.group(2)),
                inbound=float(match.group(3)),
                rssi=rssi,
            )
        )
    return hits


def collapse_round(hits: list[Hit]) -> dict[str, Hit]:
    """One sample per pubkey. Duplicate lines in the same round are averaged."""
    grouped: dict[str, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.pubkey, []).append(hit)
    out: dict[str, Hit] = {}
    for pubkey, rows in grouped.items():
        rssis = [row.rssi for row in rows if row.rssi is not None]
        out[pubkey] = Hit(
            pubkey,
            sum(row.rx for row in rows) / len(rows),
            sum(row.inbound for row in rows) / len(rows),
            sum(rssis) / len(rssis) if rssis else None,
        )
    return out


def ping_sides(rounds: int) -> list[str]:
    """A then B, repeated. Remotes share one 4-per-120s budget, so never overlap."""
    sides: list[str] = []
    for _ in range(rounds):
        sides.append("A")
        sides.append("B")
    return sides


def _shown(values: list[float]) -> int | None:
    """Nearest dB. The star uses this same integer, so it matches the cell."""
    if not values:
        return None
    return int(round(sum(values) / len(values)))


def _pair(rx: list[float], inbound: list[float]) -> str:
    rx_n = _shown(rx)
    in_n = _shown(inbound)
    if rx_n is None or in_n is None:
        return ""
    return f"{rx_n}/{in_n}"


def _points(row: ScoreRow) -> tuple[int, int]:
    """One point per direction. A miss on both numbers gives the other side the row."""
    a_rx, b_rx = _shown(row.a_rx), _shown(row.b_rx)
    a_in, b_in = _shown(row.a_in), _shown(row.b_in)
    if a_rx is None and b_rx is None:
        return 0, 0
    if a_rx is None:
        return 0, 1
    if b_rx is None:
        return 1, 0
    a_pts = int(a_rx > b_rx) + int(a_in is not None and b_in is not None and a_in > b_in)
    b_pts = int(b_rx > a_rx) + int(a_in is not None and b_in is not None and b_in > a_in)
    return a_pts, b_pts


def _row_winner(row: ScoreRow) -> str | None:
    a_pts, b_pts = _points(row)
    if a_pts > b_pts:
        return "A"
    if b_pts > a_pts:
        return "B"
    return None


def _mark(text: str, star: bool) -> str:
    if not text:
        return ""
    return f"*{text}" if star else f" {text}"


def score_rows(
    rounds_a: list[dict[str, Hit]],
    rounds_b: list[dict[str, Hit]],
    labels: dict[str, str],
) -> list[ScoreRow]:
    pubkeys: set[str] = set()
    for rnd in rounds_a + rounds_b:
        pubkeys.update(rnd)
    rows: list[ScoreRow] = []
    for pubkey in sorted(pubkeys):
        row = ScoreRow(label=neighbor_label(pubkey, labels), pubkey=pubkey)
        for rnd in rounds_a:
            hit = rnd.get(pubkey)
            if hit is not None:
                row.a_rx.append(hit.rx)
                row.a_in.append(hit.inbound)
        for rnd in rounds_b:
            hit = rnd.get(pubkey)
            if hit is not None:
                row.b_rx.append(hit.rx)
                row.b_in.append(hit.inbound)
        rows.append(row)
    return rows


def render_table(rows: list[ScoreRow], name_a: str, name_b: str) -> str:
    """One cell per board, ``rx/in``. ``*`` marks the row winner and the winning column."""
    winners = [_row_winner(row) for row in rows]
    a_wins = winners.count("A")
    b_wins = winners.count("B")
    head_a = _mark(name_a, a_wins > b_wins)
    head_b = _mark(name_b, b_wins > a_wins)
    cells = [
        (
            row.label,
            _mark(_pair(row.a_rx, row.a_in), winners[i] == "A"),
            _mark(_pair(row.b_rx, row.b_in), winners[i] == "B"),
        )
        for i, row in enumerate(rows)
    ]
    width_name = max((len(label) for label, _, _ in cells), default=0)
    width_a = max(len(head_a), *(len(cell_a) for _, cell_a, _ in cells), 1)
    width_b = max(len(head_b), *(len(cell_b) for _, _, cell_b in cells), 1)

    def line(label: str, cell_a: str, cell_b: str) -> str:
        return f"{label.ljust(width_name)}  {cell_a.rjust(width_a)}  {cell_b.rjust(width_b)}"

    lines = [
        "cell is rx/in in dB. * on a value is the stronger side. * on a name won more rows.",
        "",
        line("", head_a, head_b),
    ]
    if not cells:
        lines.append("(no neighbors answered)")
    else:
        lines.extend(line(label, cell_a, cell_b) for label, cell_a, cell_b in cells)
    return "\n".join(lines)


def parse_stats_json(text: str) -> dict:
    """Pull the first JSON object out of a stats-radio or stats-core reply."""
    import json

    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        start = line.find("{")
        end = line.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            parsed = json.loads(line[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _stat_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _snap_lines(index: int, name: str, snap: RoundSnap, labels: dict[str, str]) -> list[str]:
    noise = f"{snap.noise_floor} dBm" if snap.noise_floor is not None else "n/a"
    batt = f"{snap.battery_mv} mV" if snap.battery_mv is not None else "n/a"
    lines = [f"  {index}  {name}  noise {noise}  battery {batt}"]
    if not snap.hits:
        lines.append("     (no replies)")
        return lines
    for pubkey in sorted(snap.hits):
        hit = snap.hits[pubkey]
        rssi = f"  rssi={hit.rssi:.0f}" if hit.rssi is not None else ""
        lines.append(
            f"     {neighbor_label(pubkey, labels)}  rx={hit.rx:+.1f}  in={hit.inbound:+.1f}{rssi}"
        )
    return lines


def render_rounds(
    rounds_a: list[RoundSnap],
    rounds_b: list[RoundSnap],
    name_a: str,
    name_b: str,
    labels: dict[str, str],
    *,
    start: int = 1,
    latest_only: bool = False,
) -> str:
    """Per-round samples with this board's noise floor and battery."""
    lines = [
        "latest round" if latest_only else "rounds",
        "rx/in are SNR dB. rssi and noise are dBm. noise and battery are the board, not the neighbor.",
    ]
    if latest_only:
        pairs = []
        if rounds_a:
            pairs.append((len(rounds_a), name_a, rounds_a[-1]))
        if rounds_b:
            pairs.append((len(rounds_b), name_b, rounds_b[-1]))
    else:
        pairs = []
        for offset, (snap_a, snap_b) in enumerate(zip(rounds_a, rounds_b)):
            index = start + offset
            pairs.append((index, name_a, snap_a))
            pairs.append((index, name_b, snap_b))
    any_hit = False
    for index, name, snap in pairs:
        lines.extend(_snap_lines(index, name, snap, labels))
        if snap.hits:
            any_hit = True
    if not any_hit:
        lines.append("  (none)")
    return "\n".join(lines)


def format_radio(text: str) -> str:
    got = parse_radio(text)
    if got is None:
        return text.strip() or "unknown"
    freq, bw, sf, cr = got
    return f"{freq:g} MHz, BW {bw:g}, SF{sf}, CR {cr}"


def radios_differ(a: str, b: str) -> bool:
    left = parse_radio(a)
    right = parse_radio(b)
    if left is None or right is None:
        return a.strip() != b.strip()
    return left != right


def configs_differ(a: BoardConfig, b: BoardConfig) -> bool:
    return (
        radios_differ(a.radio, b.radio)
        or a.tx != b.tx
        or a.rxgain != b.rxgain
        or a.fem_rx != b.fem_rx
        or a.fem_tx != b.fem_tx
    )


def render_board(cfg: BoardConfig) -> str:
    tx = cfg.tx if cfg.tx == "n/a" else f"{cfg.tx} dBm"
    return "\n".join(
        [
            f"{cfg.name}  {cfg.port}",
            f"  radio   {format_radio(cfg.radio)}",
            f"  tx      {tx}",
            f"  rxgain  {cfg.rxgain}",
            f"  fem rx  {cfg.fem_rx}",
            f"  fem tx  {cfg.fem_tx}",
        ]
    )


def collapse_mac_pairs(ports: list[str]) -> list[str]:
    """Keep /dev/cu.* when macOS also lists the tty twin."""
    present = set(ports)
    out: list[str] = []
    for port in sorted(present):
        if port.startswith("/dev/tty."):
            cu = "/dev/cu." + port.removeprefix("/dev/tty.")
            if cu in present:
                continue
        out.append(port)
    return out


def app_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pyserial required for USB port detection") from exc
    want = {(spec.app_vid, spec.app_pid) for spec in TARGETS.values()}
    found: list[str] = []
    for info in list_ports.comports():
        if info.device and (info.vid, info.pid) in want:
            found.append(info.device)
    return collapse_mac_pairs(found)


def labels_from_doc(doc: dict, sites: dict | None = None) -> dict[str, str]:
    """Map an 8-hex pubkey prefix to the fleet display name."""
    from envybot.position import display_name

    nodes = doc.get("nodes") or {}
    out: dict[str, str] = {}
    if not isinstance(nodes, dict):
        return out
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        pub = str(node.get("identity_pubkey") or "").strip().lower()
        if len(pub) < 8:
            continue
        label = display_name(str(key), node, sites).strip()
        if label:
            out[pub[:8]] = label
    return out


def neighbor_label(pubkey: str, labels: dict[str, str]) -> str:
    """Book name plus the 2-byte pubkey prefix MeshCore uses as a path hash."""
    prefix = pubkey.lower()
    short = prefix[:4]
    name = labels.get(prefix, "")
    if name:
        return f"{name} ({short})"
    return f"{prefix} ({short})"


def read_round_conditions(cli: RepeaterSerial) -> tuple[int | None, int | None]:
    """Noise floor (dBm) and battery (mV) after a listen. Neither is per-neighbor."""
    radio = parse_stats_json(cli.cmd("stats-radio"))
    core = parse_stats_json(cli.cmd("stats-core"))
    return _stat_int(radio, "noise_floor"), _stat_int(core, "battery_mv")


def _require_discover_ok(text: str) -> None:
    reply = RepeaterSerial._extract_reply(text)
    if reply is None:
        raise CliError("discover.neighbors: no CLI reply (not a repeater text console?)")
    if reply_failed(reply):
        raise CliError(f"discover.neighbors: {reply}")


def _setting(cli: RepeaterSerial, command: str) -> str:
    from envybot.radio import parse_get_value

    raw = cli.cmd(command)
    if reply_failed(raw):
        return "n/a"
    return parse_get_value(raw) or "n/a"


def read_board(cli: RepeaterSerial, port: str) -> BoardConfig:
    return BoardConfig(
        name=_setting(cli, "get name"),
        port=port,
        radio=cli.cmd("get radio").strip(),
        tx=_setting(cli, "get tx"),
        rxgain=_setting(cli, "get radio.rxgain"),
        fem_rx=_setting(cli, "get radio.fem.rxgain"),
        fem_tx=_setting(cli, "get radio.fem.txgain"),
    )


def _open_pair(
    port_a: str,
    port_b: str,
    *,
    baud: int,
    timeout: float,
    verbose: bool,
) -> tuple[RepeaterSerial, RepeaterSerial]:
    first = RepeaterSerial(port_a, baud=baud, timeout=timeout, verbose=verbose)
    second = RepeaterSerial(port_b, baud=baud, timeout=timeout, verbose=verbose)
    first.__enter__()
    try:
        second.__enter__()
    except Exception:
        first.__exit__(None, None, None)
        raise
    return first, second


def wait_until(deadline: float, stop: Callable[[], bool] | None) -> bool:
    """Sleep until ``deadline``. Return True if ``stop`` fired first."""
    while True:
        if stop is not None and stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def build_report(
    *,
    board_a: BoardConfig,
    board_b: BoardConfig,
    rounds_a: list[RoundSnap],
    rounds_b: list[RoundSnap],
    labels: dict[str, str],
    listen_s: float,
    gap_s: float,
    completed: int,
    limit: int | None,
    latest_only: bool,
    footer: str | None,
) -> str:
    rows = score_rows([snap.hits for snap in rounds_a], [snap.hits for snap in rounds_b], labels)
    if limit is None:
        progress = f"live, round {completed}"
    else:
        progress = f"{completed}/{limit} rounds"
    banner = [
        render_board(board_a),
        render_board(board_b),
        "settings differ" if configs_differ(board_a, board_b) else "settings match",
        f"{progress}, listen {listen_s:g}s, {gap_s:g}s between discovers",
    ]
    parts = [
        "\n".join(banner),
        "",
        render_table(rows, board_a.name, board_b.name),
    ]
    if rounds_a or rounds_b:
        parts.extend(
            [
                "",
                render_rounds(
                    rounds_a,
                    rounds_b,
                    board_a.name,
                    board_b.name,
                    labels,
                    latest_only=latest_only,
                ),
            ]
        )
    if footer:
        parts.extend(["", footer])
    return "\n".join(parts)


def show_report(report: str, *, live: bool) -> None:
    if live and sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")
        sys.stdout.write(report)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    print(report, flush=True)


def run_shootout(
    cli_a: RepeaterSerial,
    cli_b: RepeaterSerial,
    *,
    rounds: int | None,
    listen_s: float,
    gap_s: float,
    labels: dict[str, str],
    board_a: BoardConfig,
    board_b: BoardConfig,
    live: bool = False,
    stop: Callable[[], bool] | None = None,
) -> str:
    rounds_a: list[RoundSnap] = []
    rounds_b: list[RoundSnap] = []
    saw_line = False
    next_at = time.monotonic()
    index = 0
    try:
        while rounds is None or index < rounds:
            if stop is not None and stop():
                break
            index += 1
            for side, cli, bucket in (
                (board_a.name, cli_a, rounds_a),
                (board_b.name, cli_b, rounds_b),
            ):
                if stop is not None and stop():
                    break
                if wait_until(next_at, stop):
                    break
                next_at = time.monotonic() + gap_s
                if not live:
                    label = f"{index}/{rounds}" if rounds is not None else str(index)
                    print(f"round {label}  {side}  discover.neighbors", flush=True)
                text = cli.capture("discover.neighbors", listen_s, stop)
                _require_discover_ok(text)
                hits = parse_discover_lines(text)
                noise, battery = read_round_conditions(cli)
                if hits:
                    saw_line = True
                if not live:
                    noise_txt = f"{noise} dBm" if noise is not None else "n/a"
                    batt_txt = f"{battery} mV" if battery is not None else "n/a"
                    print(f"  noise {noise_txt}  battery {batt_txt}", flush=True)
                    for hit in hits:
                        rssi = f"  rssi={hit.rssi:.0f}" if hit.rssi is not None else ""
                        print(
                            f"  {hit.pubkey}  rx={hit.rx:+.1f}  in={hit.inbound:+.1f}{rssi}",
                            flush=True,
                        )
                    if not hits:
                        print("  no discover lines", flush=True)
                bucket.append(RoundSnap(collapse_round(hits), noise, battery))
                if live:
                    show_report(
                        build_report(
                            board_a=board_a,
                            board_b=board_b,
                            rounds_a=rounds_a,
                            rounds_b=rounds_b,
                            labels=labels,
                            listen_s=listen_s,
                            gap_s=gap_s,
                            completed=index,
                            limit=rounds,
                            latest_only=True,
                            footer="q to stop",
                        ),
                        live=True,
                    )
    except KeyboardInterrupt:
        pass
    if not saw_line:
        print(
            "No discover lines. The repeater image must print "
            "`discover <pubkey8> rx= in=` (flash a current slim build).",
            file=sys.stderr,
        )
    final = build_report(
        board_a=board_a,
        board_b=board_b,
        rounds_a=rounds_a,
        rounds_b=rounds_b,
        labels=labels,
        listen_s=listen_s,
        gap_s=gap_s,
        completed=max(len(rounds_a), len(rounds_b)),
        limit=rounds,
        latest_only=live,
        footer=None,
    )
    if live:
        show_report(final, live=True)
    return final


def _resolve_ports(port_a: str | None, port_b: str | None) -> tuple[str, str]:
    if port_a and port_b:
        left = resolve_port_arg(port_a)
        right = resolve_port_arg(port_b)
        if left is None:
            raise SystemExit(f"serial port not found: {port_a}")
        if right is None:
            raise SystemExit(f"serial port not found: {port_b}")
        if left == right:
            raise SystemExit("A and B are the same serial port")
        return left, right
    if port_a or port_b:
        raise SystemExit("pass both --a and --b, or neither to autodetect two boards")
    found = app_serial_ports()
    if len(found) != 2:
        listed = ", ".join(found) if found else "none"
        raise SystemExit(
            f"need two repeater USB serial ports, found {len(found)} ({listed}). Pass --a and --b."
        )
    return found[0], found[1]


class QuitWatch:
    """cbreak stdin. `q` sets ``stopped`` without waiting for Enter."""

    def __init__(self, *, announce: bool = False) -> None:
        self.stopped = False
        self._announce = announce
        self._fd: int | None = None
        self._old: list | None = None

    def __enter__(self) -> QuitWatch:
        if not sys.stdin.isatty():
            if self._announce:
                print("stdin is not a terminal. Press Ctrl+C to stop.", file=sys.stderr)
            return self
        import threading

        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

        def loop() -> None:
            while not self.stopped:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q"):
                    self.stopped = True
                    return

        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stopped = True
        if self._fd is not None and self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


def main(book: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two USB repeaters by zero-hop neighbor discover SNR.",
    )
    parser.add_argument("--a", metavar="PORT", help="USB serial for board A")
    parser.add_argument("--b", metavar="PORT", help="USB serial for board B")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Keep sampling until q or Ctrl+C. Redraws the standings after each board.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="samples per board (default 3, or until q with --live)",
    )
    parser.add_argument("--listen", type=float, default=DEFAULT_LISTEN_S, metavar="SEC", help="seconds to collect replies")
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP_S,
        metavar="SEC",
        help="seconds from one discover to the next (default 30, shared 4/120s budget)",
    )
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=5.0, help="CLI reply timeout")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.live and args.rounds is None:
        rounds: int | None = None
    else:
        rounds = DEFAULT_ROUNDS if args.rounds is None else args.rounds
    if rounds is not None and rounds < 1:
        raise SystemExit("--rounds must be at least 1")
    if args.listen <= 0 or args.gap <= 0:
        raise SystemExit("--listen and --gap must be positive")
    if args.gap < DEFAULT_GAP_S:
        print(
            f"warning: gap {args.gap:g}s is under {DEFAULT_GAP_S:g}s. "
            "Remotes answer at most 4 discovers per 120s and will drop the rest.",
            file=sys.stderr,
        )
    if args.listen >= args.gap:
        print(
            "warning: listen is at least the gap, so discovers will run back to back.",
            file=sys.stderr,
        )

    port_a, port_b = _resolve_ports(args.a, args.b)
    labels: dict[str, str] = {}
    try:
        from envybot.book import nodes_path
        from envybot.nodes_doc import load_nodes_doc, load_sites_for_book

        path = nodes_path(book)
        if path.is_file():
            labels = labels_from_doc(load_nodes_doc(path), load_sites_for_book(path))
    except Exception as exc:
        print(f"book labels skipped: {exc}", file=sys.stderr)

    try:
        cli_a, cli_b = _open_pair(
            port_a,
            port_b,
            baud=args.baud,
            timeout=args.timeout,
            verbose=args.verbose,
        )
    except (CliError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    watch = QuitWatch(announce=args.live)
    try:
        if watch is not None:
            watch.__enter__()
        try:
            board_a = read_board(cli_a, port_a)
            board_b = read_board(cli_b, port_b)
            report = run_shootout(
                cli_a,
                cli_b,
                rounds=rounds,
                listen_s=args.listen,
                gap_s=args.gap,
                labels=labels,
                board_a=board_a,
                board_b=board_b,
                live=args.live,
                stop=lambda: watch.stopped,
            )
        except (CliError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        if watch is not None:
            watch.__exit__(None, None, None)
        cli_b.__exit__(None, None, None)
        cli_a.__exit__(None, None, None)
    if not args.live:
        print()
        print(report)
    return 0
