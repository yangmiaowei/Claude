from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from claude.core.config import ClaudeConfig
from claude.core.context import ExecutionContext
from claude.core.events.bus import EventBus, EventHandler
from claude.core.events.writer import EventWriter
from claude.core.llm.base import LLMProvider
from claude.core.llm.provider import AnthropicProvider
from claude.core.loop import AgentLoop
from claude.core.runs import RUNS_DIR, new_run_id
from claude.core.tools.builtin.read_file import ReadFileTool
from claude.core.tools.registry import ToolRegistry


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: ClaudeConfig,
        *,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR

    # 执行一次完整的 agent run: 生成 run_id、接线事件总线、驱动 AgentLoop
    async def run(self, goal: str) -> None:
        # 1. 为这次运行生成唯一 ID，创建对应目录
        run_id = new_run_id()
        run_path = self._runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)

        # 2. 建立事件总线，订阅所有监听者
        bus = EventBus()
        for h in self._extra_handlers:  # StdoutPrinter 从这里进来
            bus.subscribe(h)

        # 3. 准备 LLM、工具注册表、循环控制器
        provider = self._provider or AnthropicProvider(self._config.llm.default_model)
        register = ToolRegistry()
        register.register(ReadFileTool())
        loop = AgentLoop(provider, register, bus)

        # 4. 创建“工作记忆”，goal 在这里成为第一条消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
        )

        # 5. 打开事件文件，然后正式开始
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )
        
        if cancelled:  # EventWriter 的 async with 已经结束（文件已关闭），现在才能 re-raise
            raise asyncio.CancelledError()
