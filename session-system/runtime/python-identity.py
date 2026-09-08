"""Inspect an installed service without starting it or accessing its database."""

import importlib.metadata
import json
import os
import sys
import sysconfig

import omp_work
from omp_work.operations.database import migration_set_sha256
from omp_work.operations.fingerprints import service_runtime_fingerprint

distribution = importlib.metadata.distribution("omp-work")
direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
print(
    json.dumps(
        {
            "version": sys.version,
            "executable": os.path.realpath(sys.executable),
            "basePrefix": os.path.realpath(sys.base_prefix),
            "stdlib": os.path.realpath(sysconfig.get_path("stdlib")),
            "serviceVersion": distribution.version,
            "serviceModule": os.path.realpath(omp_work.__file__),
            "contractVersion": omp_work.CONTRACT_VERSION,
            "contractSha256": omp_work.contract_sha256(),
            "migrationSetSha256": migration_set_sha256(),
            "serviceFingerprint": service_runtime_fingerprint(),
            "editable": direct_url.get("dir_info", {}).get("editable", False),
            "distributions": sorted(
                [
                    {"name": item.metadata["Name"], "version": item.version}
                    for item in importlib.metadata.distributions()
                ],
                key=lambda item: item["name"].lower(),
            ),
        },
        sort_keys=True,
    )
)
