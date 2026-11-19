"""CVAT Local AutoQC Pre-flight Station — gói tích hợp kiểm nhãn tự động cho CVAT Local."""

from .analyzer import JobQCResult, analyze_cvat_job
from .connector import (
    CVATLocalClient,
    export_cvat_xml,
    export_yolo11_zip,
    import_yolo11_zip,
    prepare_source_dataset,
)
from .job_creator import create_rework_job, create_rework_jobs_by_issue
from .packager import build_qc_bundle
from .rework_history import (
    compute_rework_diff,
    load_rework_history,
    record_rework_history,
    save_baseline_snapshot,
)

__all__ = [
    "CVATLocalClient",
    "JobQCResult",
    "analyze_cvat_job",
    "build_qc_bundle",
    "compute_rework_diff",
    "create_rework_job",
    "create_rework_jobs_by_issue",
    "export_cvat_xml",
    "export_yolo11_zip",
    "import_yolo11_zip",
    "load_rework_history",
    "prepare_source_dataset",
    "record_rework_history",
    "save_baseline_snapshot",
]
