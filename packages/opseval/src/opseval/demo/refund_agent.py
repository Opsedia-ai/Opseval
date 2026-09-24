# SPDX-License-Identifier: Apache-2.0
"""Reference system under test: a small, deterministic online-store refund agent.

It speaks a simple JSON-over-WebSocket protocol, walks through the phases
``intake -> lookup -> proposal -> confirmation``, raises two approval checkpoints
(``proposal_approval`` and ``supervisor_review``) and finally commits ``refund_issued``.

It is rule-based on purpose: the demo needs no LLM keys, and its behaviour is
predictable enough to show what Opseval catches. ``bug`` injects realistic defects:

- ``skip-approval``   issues the refund without asking for any approval
- ``forget-method``   ignores the customer's requested refund method after a rejection
- ``crash``           drops the connection mid-conversation (a harness-level failure)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

Bug = Literal["skip-approval", "forget-method", "crash"]
BUGS: tuple[str, ...] = ("skip-approval", "forget-method", "crash")

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
ORDER_RE = re.compile(r"\d{4,}")
ARABIC_RE = re.compile(r"[؀-ۿ]")
CARD_WORDS = ("card", "original", "visa", "كارت", "بطاقة", "البطاقة", "الفيزا", "فيزا")

CATALOGUE: dict[str, dict[str, Any]] = {
    "10482": {"item": "blender", "item_ar": "خلاط", "amount": 89.99, "currency": "GBP"},
    "48213": {"item": "blender", "item_ar": "خلاط", "amount": 89.99, "currency": "GBP"},
    "55120": {"item": "headphones", "item_ar": "سماعات", "amount": 249.00, "currency": "SAR"},
}

TEXT = {
    "ask_order": (
        "Sorry to hear that. Could you share your order number?",
        "آسفين جداً على كده. ممكن رقم الطلب؟",
    ),
    "unknown_order": (
        "I couldn't find that order. Could you double-check the number?",
        "مش لاقي الطلب ده. ممكن تتأكد من الرقم؟",
    ),
    "found": (
        "I found order #{order}: {item}, paid {amount} {currency}.",
        "لقيت الطلب رقم {order}: {item_ar} بمبلغ {amount} {currency}.",
    ),
    "eligible": ("The item is eligible for a refund.", "المنتج ينفع يترجع."),
    "propose": (
        "I can refund {amount} {currency} as {method_label}. Do you approve?",
        "أقدر أرجعلك {amount} {currency} {method_label_ar}. موافق؟",
    ),
    "updating": ("Understood, let me update the proposal.", "تمام، هعدّل العرض."),
    "approved": (
        "Thanks! A supervisor will review the refund now.",
        "شكراً! المشرف هيراجع طلب الاسترجاع دلوقتي.",
    ),
    "issued": ("Your refund has been issued.", "تم إصدار الاسترجاع."),
    "cancelled": (
        "The refund was cancelled. Anything else I can help with?",
        "تم إلغاء الاسترجاع. أقدر أساعدك في حاجة تانية؟",
    ),
    "anything_else": ("Is there anything else I can help with?", "في حاجة تانية أقدر أساعدك فيها؟"),
}

METHOD_LABEL = {
    "store_credit": ("store credit", "رصيد في المتجر"),
    "original_card": ("a refund to your original card", "على الكارت الأصلي"),
}


@dataclass
class Conversation:
    bug: Bug | None
    session_id: str = field(default_factory=lambda: f"s-{uuid.uuid4().hex[:8]}")
    phase: str = "intake"
    arabic: bool = False
    order: str | None = None
    method: str = "store_credit"
    pending: str | None = None
    turns: int = 0
    customer_text: list[str] = field(default_factory=list)

    def t(self, key: str, **kw: Any) -> str:
        en, ar = TEXT[key]
        return (ar if self.arabic else en).format(**kw)

    def message(self, text: str, *, expects_reply: bool = False, **data: Any) -> dict[str, Any]:
        frame: dict[str, Any] = {"type": "message", "phase": self.phase, "text": text}
        if data:
            frame["data"] = data
        if expects_reply:
            frame["expects_reply"] = True
        return frame

    # --- behaviour ------------------------------------------------------------------
    def on_user_message(self, text: str) -> list[dict[str, Any]]:
        self.turns += 1
        self.customer_text.append(text)
        if ARABIC_RE.search(text):
            self.arabic = True
        if any(w in text.lower() for w in CARD_WORDS):
            self.method = "original_card"
        if self.pending is not None:
            # Chatting while a checkpoint is pending counts as a rejection with feedback.
            return self.on_resume(self.pending, "reject", text)
        if self.phase == "intake":
            match = ORDER_RE.search(text.translate(ARABIC_DIGITS))
            if not match:
                return [self.message(self.t("ask_order"), expects_reply=True)]
            if match.group(0) not in CATALOGUE:
                return [self.message(self.t("unknown_order"), expects_reply=True)]
            self.order = match.group(0)
            return self._lookup_and_propose()
        return [self.message(self.t("anything_else"), expects_reply=True)]

    def _order_info(self) -> dict[str, Any]:
        assert self.order is not None
        return {"order": self.order, **CATALOGUE[self.order]}

    def _lookup_and_propose(self) -> list[dict[str, Any]]:
        info = self._order_info()
        self.phase = "lookup"
        frames = [
            self.message(self.t("found", **info), order_summary=info),
            self.message(self.t("eligible"), eligibility={"order": self.order, "eligible": True}),
        ]
        if self.bug == "skip-approval":
            self.phase = "confirmation"
            return [*frames, self._issue_refund()]
        return [*frames, *self._propose()]

    def _propose(self) -> list[dict[str, Any]]:
        info = self._order_info()
        self.phase = "proposal"
        self.pending = "proposal_approval"
        label, label_ar = METHOD_LABEL[self.method]
        proposal = {
            "order": self.order,
            "amount": info["amount"],
            "currency": info["currency"],
            "method": self.method,
        }
        text = self.t(
            "propose",
            amount=info["amount"],
            currency=info["currency"],
            method_label=label,
            method_label_ar=label_ar,
        )
        return [
            self.message(text, refund_proposal=proposal),
            {
                "type": "needs_human",
                "phase": "proposal",
                "checkpoint": "proposal_approval",
                "payload": {"refund_proposal": proposal},
            },
        ]

    def _issue_refund(self) -> dict[str, Any]:
        info = self._order_info()
        self.pending = None
        return {
            "type": "result",
            "phase": "confirmation",
            "outcome": "refund_issued",
            "text": self.t("issued"),
            "data": {
                "refund_status": {
                    "order": self.order,
                    "amount": info["amount"],
                    "method": self.method,
                    "status": "issued",
                }
            },
        }

    def on_resume(self, checkpoint: str, action: str, feedback: str | None) -> list[dict[str, Any]]:
        if checkpoint != self.pending:
            return [
                {
                    "type": "error",
                    "code": "no_pending_checkpoint",
                    "message": f"no pending checkpoint {checkpoint!r}",
                }
            ]
        if feedback and any(w in feedback.lower() for w in CARD_WORDS):
            self.method = "original_card"
        if checkpoint == "proposal_approval":
            if action == "approve":
                self.phase = "confirmation"
                self.pending = "supervisor_review"
                info = self._order_info()
                return [
                    self.message(self.t("approved")),
                    {
                        "type": "needs_human",
                        "phase": "confirmation",
                        "checkpoint": "supervisor_review",
                        "payload": {
                            "proposal_status": "approved_by_customer",
                            "amount": info["amount"],
                            "method": self.method,
                        },
                    },
                ]
            if self.bug == "forget-method":
                self.method = "store_credit"
            self.phase = "proposal"
            return [self.message(self.t("updating")), *self._propose()]
        if checkpoint == "supervisor_review":
            if action == "approve":
                return [self._issue_refund()]
            self.pending = None
            return [self.message(self.t("cancelled"), expects_reply=True)]
        return [{"type": "error", "code": "unknown_checkpoint", "message": checkpoint}]


async def _handle(conn: ServerConnection, bug: Bug | None) -> None:
    conv = Conversation(bug=bug)
    await conn.send(json.dumps({"type": "session_created", "session_id": conv.session_id}))
    try:
        async for raw in conn:
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await conn.send(json.dumps({"type": "error", "code": "bad_json"}))
                continue
            kind = frame.get("type") if isinstance(frame, dict) else None
            if kind == "auth":
                continue
            if kind == "user_message":
                replies = conv.on_user_message(str(frame.get("text", "")))
            elif kind == "resume":
                replies = conv.on_resume(
                    str(frame.get("checkpoint")), str(frame.get("action")), frame.get("feedback")
                )
            else:
                replies = [{"type": "error", "code": "unknown_frame", "message": str(kind)}]
            for reply in replies:
                await conn.send(json.dumps(reply, ensure_ascii=False))
            if bug == "crash" and conv.turns >= 1:
                await conn.close(code=1011, reason="simulated crash")
                return
    except ConnectionClosed:
        return


@asynccontextmanager
async def run_agent(
    host: str = "127.0.0.1", port: int = 0, bug: Bug | None = None
) -> AsyncIterator[str]:
    """Run the agent in the background; yields its ``ws://`` URL."""
    server: Server = await serve(lambda c: _handle(c, bug), host, port)
    try:
        sock_port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://{host}:{sock_port}"
    finally:
        server.close()
        await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description="Opseval reference refund agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bug", choices=BUGS, default=None)
    args = parser.parse_args()

    async def _run() -> None:
        async with run_agent(args.host, args.port, args.bug) as url:
            print(f"refund agent listening on {url} (bug={args.bug})")
            await asyncio.Future()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
