package swiftdeploy.infrastructure

import data.thresholds

default allow = false

allow if {
    input.disk_free_gb >= thresholds.min_disk_free_gb
    input.cpu_load <= thresholds.max_cpu_load
}

violations contains msg if {
    input.disk_free_gb < thresholds.min_disk_free_gb
    msg := sprintf(
        "Disk free %.1fGB is below minimum %.1fGB",
        [input.disk_free_gb, thresholds.min_disk_free_gb],
    )
}

violations contains msg if {
    input.cpu_load > thresholds.max_cpu_load
    msg := sprintf(
        "CPU load %.2f exceeds maximum %.2f",
        [input.cpu_load, thresholds.max_cpu_load],
    )
}
