"""Streamlit demo UI.

Run:
    streamlit run app/streamlit_app.py

The UI is intentionally thin - it calls the same `run_pipeline` entry point the
API uses, so what you see here is exactly what the pipeline produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import get_settings  # noqa: E402
from sourced_intel.graph import run_pipeline  # noqa: E402

st.set_page_config(page_title="Sourced Intel", page_icon="◈", layout="wide")

SAMPLES = [
    "深圳市储能产业最近三个月的公开动向",
    "What are the latest energy storage project developments in Shenzhen over the last 3 months?",
    "广东省光伏项目的投资和产能情况",
]


@st.cache_data(show_spinner=False)
def _settings_fingerprint() -> dict:
    return get_settings().redacted()


def _show_summary(summary: dict) -> None:
    cols = st.columns(5)
    cols[0].metric("Status", summary["status"])
    cols[1].metric("Elapsed", f"{summary['elapsed_seconds']}s")
    cols[2].metric("Documents", summary["raw_docs"])
    cols[3].metric("Records", summary["intel_items"])
    cols[4].metric("Failed sources", len(summary["failed_sources"]))

    trace = summary.get("trace_summary") or {}
    st.caption(
        f"{trace.get('nodes_executed', 0)} nodes executed · "
        f"{trace.get('total_duration_ms', 0)} ms total · "
        f"{trace.get('total_tokens', 0)} LLM tokens · "
        f"slowest node: {trace.get('slowest_node')}"
    )


def main() -> None:
    st.title("◈ Sourced Intel")
    st.caption(
        "One sentence in, a sourced intelligence report out. "
        "Every claim links back to the public document it came from."
    )

    with st.sidebar:
        st.subheader("Configuration")
        cfg = _settings_fingerprint()
        st.write(
            {
                "provider": cfg["llm_provider"],
                "model": cfg["glm_model"] if cfg["llm_provider"] == "glm" else cfg["ollama_model"],
                "api key": cfg["glm_api_key"],
                "confidence threshold": cfg["confidence_threshold"],
                "gdelt pacing (s)": cfg["gdelt_min_interval"],
            }
        )
        st.divider()
        st.subheader("Sample queries")
        for sample in SAMPLES:
            if st.button(sample, use_container_width=True):
                st.session_state["query"] = sample
        st.divider()
        st.caption(
            "Sources: GDELT DOC 2.0 (breadth) + china5e.com (depth, full article body) + "
            "DataV GeoAtlas (boundaries)."
        )

    query = st.text_input(
        "Ask a question",
        value=st.session_state.get("query", SAMPLES[0]),
        placeholder="e.g. energy storage developments in Shenzhen over the last 3 months",
    )

    col_run, col_tip = st.columns([1, 4])
    run_clicked = col_run.button("Run pipeline", type="primary", use_container_width=True)
    col_tip.caption(
        "The first GDELT request may take up to a minute: the API rate-limits shared "
        "IPs, so the adapter retries with backoff and the run continues without it."
    )

    if run_clicked and query.strip():
        progress = st.progress(0, text="1/7 parsing intent...")
        with st.status("Running the pipeline", expanded=True) as status:
            st.write("Parsing intent, retrieving sources, extracting records...")
            progress.progress(30, text="3/7 extracting structured records...")
            try:
                result = run_pipeline(query.strip())
            except Exception as exc:
                status.update(label="Pipeline failed", state="error")
                st.exception(exc)
                return
            progress.progress(100, text="7/7 done")
            status.update(label=f"Run {result['summary']['run_id']} finished", state="complete")

        summary = result["summary"]
        st.session_state["result"] = result

        if summary.get("clarification"):
            st.warning(f"**Clarification needed:** {summary['clarification']}")
            return

        _show_summary(summary)

        if summary["failed_sources"]:
            st.warning(
                "Sources that failed and were skipped: "
                + ", ".join(summary["failed_sources"])
                + " — the report notes this explicitly."
            )

        report_path = summary.get("report_path")
        if report_path and Path(report_path).exists():
            tabs = st.tabs(["Report", "Structured records", "Pipeline internals"])
            with tabs[0]:
                html = Path(report_path).read_text(encoding="utf-8")
                st.download_button(
                    "Download report.html",
                    html,
                    file_name=f"{summary['run_id']}_report.html",
                    mime="text/html",
                )
                st.components.v1.html(html, height=1000, scrolling=True)
            with tabs[1]:
                items = result["state"].get("intel_items") or []
                st.dataframe(
                    [
                        {
                            "title": i.get("title"),
                            "subject": i.get("subject"),
                            "amount_raw": i.get("amount_raw"),
                            "event_time": (i.get("event_time") or "")[:10],
                            "event_type": i.get("event_type"),
                            "confidence": i.get("confidence"),
                            "needs_review": i.get("needs_review"),
                        }
                        for i in items
                    ],
                    use_container_width=True,
                )
                st.caption(f"{len(items)} record(s) extracted.")
            with tabs[2]:
                st.markdown("**Run artefacts**")
                st.code(
                    f"{summary['run_dir']}/\n"
                    f"├── report.html\n"
                    f"├── trace.jsonl        # per-node timing and token usage\n"
                    f"└── snapshots/         # state after every node\n"
                    f"    " + "\n    ".join(summary["snapshots"]),
                    language="text",
                )
                st.markdown("**Per-node metadata**")
                st.json(summary["node_meta"])
                st.markdown("**Trace**")
                trace_path = Path(summary["run_dir"]) / "trace.jsonl"
                if trace_path.exists():
                    st.code(trace_path.read_text(encoding="utf-8"), language="json")
        else:
            st.error("This run produced no report. Check the errors below.")
            st.json(summary.get("errors"))

    elif st.session_state.get("result"):
        result = st.session_state["result"]
        _show_summary(result["summary"])
        report_path = result["summary"].get("report_path")
        if report_path and Path(report_path).exists():
            st.components.v1.html(
                Path(report_path).read_text(encoding="utf-8"), height=900, scrolling=True
            )


if __name__ == "__main__":
    main()
