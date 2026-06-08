"""Agent core — plan -> select tools -> execute -> result, then trust-gate it.

Deliberately thin: the LLM plans (choosing from the pack rendered in-context) and
emits executable code; the executor runs it; the judge scores trust; the calibrated
gate decides auto-accept vs. escalate. The value Caliper adds over a bare agent is
the last two steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..util import extract_json, parse_caliper_result
from .executor import Executor
from .provenance import ProvenanceLog
from .registry import Pack

PLAN_SYSTEM = (
    "You are Caliper, a careful scientific analysis planner. Given a task, the input "
    "data files, and a catalogue of available tools, produce a concrete, reproducible "
    "plan. Prefer the catalogue's tools where they are installed. Each step must include "
    "runnable Python `code` that reads inputs from the JSON in env var CALIPER_INPUTS (a "
    "list of {'path','label'}), performs the step, and prints its result on a single line "
    "beginning with `CALIPER_RESULT:` immediately followed by compact JSON. Do not invent "
    "file paths. Write all outputs and temp files to the current working directory (the "
    "confined workspace); input files are READ-ONLY — never modify or delete them."
)

# What the executor can actually run. Stating this prevents the planner from emitting
# code that depends on tools that aren't present (e.g. reaching for R/DESeq2 via rpy2).
DEFAULT_ENVIRONMENT = (
    "Python 3 with numpy, pandas, scipy, and the standard library ONLY. R / rpy2 / "
    "Bioconductor (incl. DESeq2/edgeR) and external CLI tools are NOT installed and will "
    "fail to import. Do a pure-Python differential-expression analysis instead, and make "
    "the FINAL step print CALIPER_RESULT with the differentially-expressed gene list "
    "(fields: n_genes, n_de, and top = [{gene, log2fc, pvalue}])."
)


@dataclass
class Step:
    tool: str
    rationale: str
    code: str


@dataclass
class CaliperResult:
    task: str
    answer: dict
    trust: float
    accepted: bool
    decision: str  # "auto-accept" | "escalate"
    steps: List[Step] = field(default_factory=list)
    raw_stdout: str = ""
    provenance_path: Optional[str] = None


class CaliperAgent:
    def __init__(self, pack: Pack, llm, judge=None, gate=None,
                 executor: Optional[Executor] = None,
                 provenance: Optional[ProvenanceLog] = None,
                 feedback=None, alpha: float = 0.10, delta: float = 0.05,
                 environment: Optional[str] = None, repair: bool = True):
        from ..trust.judge import Judge  # local import avoids cycle
        self.pack = pack
        self.llm = llm
        self.environment = environment or DEFAULT_ENVIRONMENT
        self.judge = judge or Judge(llm)
        self.gate = gate  # CalibratedGate or None (uncalibrated => always escalate)
        self.executor = executor or Executor()
        self.provenance = provenance or ProvenanceLog()
        # Live mutualistic loop: if a feedback store is attached, the gate is
        # re-fit from all accumulated expert adjudications before every decision,
        # so each correction immediately tightens the gate (recalibration is ~ms).
        self.feedback = feedback
        self.alpha = alpha
        self.delta = delta
        self.repair = repair

    def _plan_prompt(self, task: str, data_files: List[dict]) -> str:
        files = "\n".join(f"  - {f.get('label', '?')}: {f['path']}" for f in data_files)
        return (
            f"{self.pack.as_context()}\n\n"
            f"# Execution environment\n{self.environment}\n\n"
            f"# Task\n{task}\n\n"
            f"# Input data files\n{files}\n\n"
            f"Return JSON: {{\"summary\": str, \"steps\": "
            f"[{{\"tool\": str, \"rationale\": str, \"code\": str}}]}}.\n"
            f"RESPOND_WITH: plan_json"
        )

    def _repair(self, task: str, data_files: List[dict], stdout: str):
        """Single corrective attempt: re-prompt with the failure, get one fixed step."""
        prompt = (
            f"# Execution environment\n{self.environment}\n\n"
            f"# Task\n{task}\n\n"
            f"# Your previous attempt produced NO parseable result. Output/errors:\n"
            f"{stdout[-1500:]}\n\n"
            f"Write ONE self-contained Python script for this environment that completes "
            f"the task and prints, on a single final line, `CALIPER_RESULT:` immediately "
            f"followed by compact JSON. Reads inputs from env CALIPER_INPUTS.\n"
            f"Return JSON: {{\"summary\": str, \"steps\": "
            f"[{{\"tool\": str, \"rationale\": str, \"code\": str}}]}}.\n"
            f"RESPOND_WITH: plan_json"
        )
        plan = extract_json(self.llm.complete(prompt, system=PLAN_SYSTEM))
        steps = plan.get("steps", [])
        if not steps:
            return None
        s = steps[0]
        return Step(tool=s.get("tool", "repair"), rationale=s.get("rationale", "repair retry"),
                    code=s.get("code", ""))

    def run(self, task: str, data_files: List[dict]) -> CaliperResult:
        plan = extract_json(self.llm.complete(self._plan_prompt(task, data_files),
                                              system=PLAN_SYSTEM))
        steps = [Step(tool=s.get("tool", "?"), rationale=s.get("rationale", ""),
                      code=s.get("code", "")) for s in plan.get("steps", [])]

        stdout, answer = "", {}
        for st in steps:
            res = self.executor.run(st.code, inputs=data_files)
            stdout += res.stdout
            if res.stderr:
                stdout += f"\n[stderr] {res.stderr}"
            parsed = parse_caliper_result(res.stdout)
            if parsed is not None:
                answer = parsed

        # One corrective retry: some models fail to emit the result contract or call a
        # missing tool. Show them the failure and ask for a single fixed script.
        if not answer and self.repair:
            fixed = self._repair(task, data_files, stdout)
            if fixed is not None:
                steps.append(fixed)
                res = self.executor.run(fixed.code, inputs=data_files)
                stdout += res.stdout + (f"\n[stderr] {res.stderr}" if res.stderr else "")
                parsed = parse_caliper_result(res.stdout)
                if parsed is not None:
                    answer = parsed

        trust = self.judge.score(task, steps, answer, stdout)
        if self.feedback is not None and len(self.feedback) > 0:
            self.gate = self.feedback.recalibrate(self.alpha, self.delta)
        accepted = bool(self.gate and self.gate.decide(trust).accept)
        decision = "auto-accept" if accepted else "escalate"

        result = CaliperResult(task=task, answer=answer, trust=trust,
                               accepted=accepted, decision=decision,
                               steps=steps, raw_stdout=stdout)
        result.provenance_path = self.provenance.record(result, self.pack.name)
        return result
