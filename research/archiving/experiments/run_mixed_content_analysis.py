"""
Analysis runner to evaluate WARC mixed-content classification and filtering.
Outputs structured metrics to results/warc_mixed_content_analysis.json.
"""

import json
import os
import sys
from typing import Any, Dict, List

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from research.archiving.experiments.inspect_warc_records import (
    classify_record_content,
    filter_usable_html_records,
    inspect_warc_file,
)


def run_analysis(warc_path: str, output_json: str) -> Dict[str, Any]:
    records = inspect_warc_file(warc_path)

    total_records = len(records)
    response_records = sum(1 for r in records if r["warc_type"] == "response")

    counts = {
        "HTML": 0,
        "CSS": 0,
        "JAVASCRIPT": 0,
        "IMAGE": 0,
        "JSON": 0,
        "TEXT": 0,
        "FONT": 0,
        "XML": 0,
        "PDF": 0,
        "OTHER": 0,
    }

    for r in records:
        cat = r["classified_category"]
        counts[cat] = counts.get(cat, 0) + 1

    filtered_html = filter_usable_html_records(warc_path)

    # Expected ground truth for mixed_content_test.warc:
    # Exactly 3 HTML URLs
    expected_html_uris = {
        "https://example.test/articles/science-today.html",
        "https://example.test/article/123",
        "https://example.test/news?id=45",
    }

    retrieved_html_uris = {r["warc_target_uri"] for r in filtered_html}

    false_positives = list(retrieved_html_uris - expected_html_uris)
    false_negatives = list(expected_html_uris - retrieved_html_uris)
    misclassified = false_positives + false_negatives

    # Accuracy: (Total - Misclassified) / Total expected evaluation items
    correct_classifications = (
        len(expected_html_uris.intersection(retrieved_html_uris))
        + (total_records - len(expected_html_uris) - len(false_positives))
    )
    accuracy = round(correct_classifications / total_records, 4) if total_records > 0 else 0.0

    analysis_result = {
        "warc_file": os.path.basename(warc_path),
        "total_records": total_records,
        "response_records": response_records,
        "html_records": counts["HTML"],
        "css_records": counts["CSS"],
        "javascript_records": counts["JAVASCRIPT"],
        "image_records": counts["IMAGE"],
        "json_records": counts["JSON"],
        "text_records": counts["TEXT"],
        "font_records": counts["FONT"],
        "other_records": counts["OTHER"],
        "filtered_usable_html_count": len(filtered_html),
        "expected_html_count": len(expected_html_uris),
        "html_filter_accuracy": accuracy,
        "misclassified_records": misclassified,
        "classification_breakdown": [
            {
                "index": r["index"],
                "warc_type": r["warc_type"],
                "target_uri": r["warc_target_uri"],
                "http_status": r["http_status"],
                "http_content_type": r["http_content_type"],
                "classified_category": r["classified_category"],
                "payload_size_bytes": r["payload_size_bytes"],
            }
            for r in records
        ],
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(analysis_result, f, indent=2)

    return analysis_result


if __name__ == "__main__":
    warc_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "warc", "mixed_content_test.warc"
    )
    out_json = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "results", "warc_mixed_content_analysis.json"
    )

    print(f"Running mixed-content analysis on {warc_path}...")
    res = run_analysis(warc_path, out_json)
    print(f"Saved analysis to {out_json}")
    print(f"Total records: {res['total_records']}, HTML records: {res['html_records']}")
    print(f"HTML filter accuracy: {res['html_filter_accuracy'] * 100}%")
    print(f"Misclassified records: {res['misclassified_records']}")
