"""CDK app entrypoint: `npx aws-cdk@2 synth` from infra/ (see `make synth`).

Deliberately environment-agnostic: no account, no region, and therefore no
`fromLookup` anywhere — the synth runs with no AWS credentials present,
which is the Phase 4 gate. cdk-nag's AwsSolutionsChecks runs as an aspect
over the whole app; an unsuppressed error fails the synth, so `cdk synth`
green implies nag clean.
"""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from tax_tables_stack import TaxTablesStack

app = cdk.App()
TaxTablesStack(app, "TaxTables")
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
