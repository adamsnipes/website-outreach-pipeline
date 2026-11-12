"""Pipeline stages.  Deterministic Python owns all orchestration."""

from outreach.pipeline.orchestrator import PipelineResult, RunContext, run_pipeline

__all__ = ["PipelineResult", "RunContext", "run_pipeline"]
