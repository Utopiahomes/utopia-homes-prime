"""Host cards: how guest messages that need Ray or Meghan reach them on Telegram, one at a time.

A *card* is one guest message that needs a person, shown with Lucy's proposed reply, or a
learning card (business_core/learning.py) asking to save something Lucy learned. The rules:

- **One card on screen.** The latest card sent is the one on screen, and a plain reply ("send",
  "tell them yes") applies to it. The code decides which card an answer is for, never the model;
  swiping to reply to an older card names that card instead.
- **Urgency, not age.** Urgent first (something broken, unsafe, or blocking a guest who is
  there or arriving), then today (a guest arriving or staying soon), then normal, oldest first
  within each. Lucy suggests a level; code can raise it, never lower it.
- **Urgent cuts in.** An urgent card is sent at once even while another card is on screen, unless
  the host is mid-answer on that card (active in the last two minutes). When the urgent one is
  done, the interrupted card is sent again.
- **Only "send" finalizes, and only the wording on screen.** A change from the host becomes a new
  version of the same card, sent back for a "send".
- **Reminders.** An unanswered card is sent again: urgent every 15 minutes at any hour; today
  every hour and normal every 3 hours, but never between 10 PM and 9 AM Eastern (an overnight
  card first goes out at 9 AM).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from utopia_homes_prime.business_core.guest import GuestDesk, GuestTurn, Reservation

Priority = Literal["urgent", "today", "normal"]
RANK: dict[str, int] = {"urgent": 0, "today": 1, "normal": 2}
EMOJI = {"urgent": "🚨 URGENT", "today": "⏰ Today", "normal": "🗓 Normal"}
REMIND_EVERY = {
    "urgent": timedelta(minutes=15),
    "today": timedelta(hours=1),
    "normal": timedelta(hours=3),
}
ENGAGED = timedelta(minutes=2)
OPEN_STATES = ("queued", "escalated")
_PLACEHOLDER = re.compile(r"\[[^\]]{1,80}\]")

# Words that make a guest message urgent whatever Lucy thinks.
URGENT_WORDS = re.compile(
    r"(?i)\b(leak\w*|flood\w*|water (?:everywhere|all over)|no (?:hot )?water|no heat|"
    r"heat(?:er|ing)? (?:is ?n[o']t|not|stopped) working|no (?:ac|a/c|air)|"
    r"(?:ac|a/c|air conditioning) (?:is ?n[o']t|not|stopped) working|locked out|"
    r"can'?t get in|cannot get in|won'?t open|smoke|on fire|fire alarm|gas smell|"
    r"smell(?:s)? (?:of )?gas|"
    r"carbon monoxide|co alarm|injur\w*|hurt|ambulance|police|break-?in|"
    r"broken (?:window|door|lock)|"
    r"power (?:is )?out|no power|no electricity|sewage|overflow\w*|emergency)\b"
)


def eastern_now(now: datetime) -> datetime:
    """US Eastern wall-clock time for a UTC instant (DST from the second Sunday of March to the
    first Sunday of November, 2 AM local), without depending on system time-zone data."""
    now = now.astimezone(UTC)

    def nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    starts = datetime.combine(nth_sunday(now.year, 3, 2), datetime.min.time(), UTC) + timedelta(
        hours=7
    )
    ends = datetime.combine(nth_sunday(now.year, 11, 1), datetime.min.time(), UTC) + timedelta(
        hours=6
    )
    offset = timedelta(hours=-4) if starts <= now < ends else timedelta(hours=-5)
    return (now + offset).replace(tzinfo=None)


def quiet(now: datetime) -> bool:
    """10 PM to 9 AM Eastern: only urgent cards go out."""
    hour = eastern_now(now).hour
    return hour >= 22 or hour < 9


def priority_for(
    suggested: str | None, guest_message: str, reservation: Reservation, now: datetime
) -> Priority:
    """Lucy's suggestion, raised (never lowered) by the words in the message and by timing: a
    guest who is staying or arrives within a day is at least `today`."""
    level: Priority = suggested if suggested in RANK else "normal"  # type: ignore[assignment]
    today = eastern_now(now).date()
    around = reservation.check_in - timedelta(days=1) <= today <= reservation.check_out
    if URGENT_WORDS.search(guest_message) and around:
        return "urgent"
    if URGENT_WORDS.search(guest_message) and RANK[level] > RANK["today"]:
        level = "today"
    if around and RANK[level] > RANK["today"]:
        level = "today"
    return level


def order(turns: Iterable[GuestTurn]) -> list[GuestTurn]:
    return sorted(turns, key=lambda t: (RANK[t.priority], t.created_at))


def card_text(turn: GuestTurn, home: str, waiting: list[GuestTurn], problem: str = "") -> str:
    urgent = sum(1 for t in waiting if t.priority == "urgent")
    position = f"1 of {len(waiting)} waiting" + (f", {urgent} urgent" if urgent else "")
    lines = [
        f"{EMOJI[turn.priority]} · {home} · {position}",
        "",
        f"Guest: {turn.guest_message}",
        "",
    ]
    if problem:
        lines += [f"⚠️ {problem}", ""]
    if turn.escalation:
        lines += [f"Lucy is handing this to you ({turn.escalation.get('category')}): "
                  f"{turn.escalation.get('reason')}", ""]  # fmt: skip
    last = turn.attempts[-1] if turn.attempts else None
    version = len(turn.attempts)
    if last is not None:
        if last.author == "host":
            label = f"Your updated reply (v{version})"
        elif last.decision == "block":
            label = "Blocked draft (needs rewording)"
        else:
            label = "Proposed reply"
        lines += [f"{label}:", last.text, ""]
        notes = [f["detail"] for f in last.findings]
        if notes:
            lines += ["Check notes: " + "; ".join(notes), ""]
    if turn.proposed_work:
        lines += [f"Lucy also suggested a work item ({', '.join(turn.proposed_work)}).", ""]
    sendable = last is not None and last.decision != "block" and not _PLACEHOLDER.search(last.text)
    if sendable:
        lines.append('Reply "send", your changes, or "reject".')
    else:
        lines.append('Reply with the reply you want, or "reject".')
    lines.append(f"[{turn.id} v{version}]")
    return "\n".join(lines)


class CardDispatcher:
    """Decides which card goes to the hosts and when. `tick` is called after every change and
    once a minute; it sends at most one card per call."""

    def __init__(
        self,
        desk: GuestDesk,
        send: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._desk = desk
        self._send = send
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def poke(self, delay: float = 0.0) -> None:
        """Run a tick now, or after `delay` seconds (after an answer, so Lucy's confirmation
        reaches the chat before the next card does)."""
        if delay > 0:
            timer = threading.Timer(delay, self._wake.set)
            timer.daemon = True
            timer.start()
        else:
            self._wake.set()

    def start(self) -> None:
        def loop() -> None:
            while True:
                self._wake.wait(timeout=60)
                self._wake.clear()
                try:
                    self.tick()
                except Exception:  # the loop must survive a bad tick
                    import logging

                    logging.getLogger(__name__).warning("card dispatch failed", exc_info=True)

        threading.Thread(target=loop, daemon=True, name="host-cards").start()

    def tick(self) -> Any:
        """Guest cards first (urgent cutting in); learning cards only when no guest card waits;
        quiet hours for everything but urgent; reminders for guest cards only."""
        with self._lock:
            now = self._clock()
            recent = self._desk.store.turns(None, limit=300)
            waiting = order(t for t in recent if t.state in OPEN_STATES)
            learning = self._desk.learning
            lessons = learning.store.recent(200) if learning is not None else []
            open_lessons = sorted((c for c in lessons if c.state == "open"),
                                  key=lambda c: c.created_at)  # fmt: skip
            shown: list[Any] = [t for t in recent if t.card_sent_at]
            shown += [c for c in lessons if c.card_sent_at]
            on_screen = max(shown, key=lambda c: c.card_seq) if shown else None
            screen_open = on_screen is not None and (
                on_screen.state in OPEN_STATES
                if on_screen.id.startswith("gt-")
                else on_screen.state == "open"
            )
            allowed = [t for t in waiting if t.priority == "urgent" or not quiet(now)]
            if not screen_open:
                if allowed:
                    return self.send_card(allowed[0], waiting, now)
                if open_lessons and not waiting and not quiet(now):
                    return self.send_card(open_lessons[0], waiting, now)
                return None
            assert on_screen is not None
            engaged = (
                on_screen.host_active_at is not None and now - on_screen.host_active_at < ENGAGED
            )
            if on_screen.id.startswith("lc-"):
                # A waiting guest always outranks a lesson (once the host pauses).
                if allowed and not engaged:
                    return self.send_card(allowed[0], waiting, now)
                return None
            if on_screen.priority != "urgent" and not engaged:
                unseen_urgent = [
                    t for t in waiting if t.priority == "urgent" and t.id != on_screen.id
                ]
                if unseen_urgent:
                    return self.send_card(unseen_urgent[0], waiting, now)
            due = (on_screen.card_sent_at or now) + REMIND_EVERY[on_screen.priority] <= now
            if due and not engaged and (on_screen.priority == "urgent" or not quiet(now)):
                return self.send_card(on_screen, waiting, now)
            return None

    def send_card(
        self,
        card: Any,
        waiting: list[GuestTurn] | None = None,
        now: datetime | None = None,
        problem: str = "",
    ) -> Any:
        now = now or self._clock()
        seq = self._desk.next_card_seq()
        if card.id.startswith("lc-"):
            from utopia_homes_prime.business_core.learning import learning_card_text

            home = self._desk.home_name(card.property_slug) if card.property_slug else "Utopia"
            self._send(learning_card_text(card, home))
            assert self._desk.learning is not None
            return self._desk.learning.mark_card_sent(card.id, now, seq)
        if waiting is None:
            waiting = order(t for t in self._desk.store.turns(OPEN_STATES, 300))
        home = self._desk.home_name(card.property_slug)
        self._send(card_text(card, home, waiting or [card], problem))
        return self._desk.mark_card_sent(card.id, now, seq)
