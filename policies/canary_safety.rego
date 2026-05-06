package swiftdeploy.canary_safety

import data.thresholds

default allow = false

allow if {
    input.error_rate <= thresholds.max_error_rate
    input.p99_latency_ms <= thresholds.max_p99_latency_ms
}

violations contains msg if {
    input.error_rate > thresholds.max_error_rate
    msg := sprintf(
        "Error rate %.2f%% exceeds maximum %.2f%%",
        [input.error_rate * 100, thresholds.max_error_rate * 100],
    )
}

violations contains msg if {
    input.p99_latency_ms > thresholds.max_p99_latency_ms
    msg := sprintf(
        "P99 latency %dms exceeds maximum %dms",
        [input.p99_latency_ms, thresholds.max_p99_latency_ms],
    )
}
