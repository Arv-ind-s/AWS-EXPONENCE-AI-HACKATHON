# Security policy

Covenant Radar is designed for sensitive commercial-credit information. The
security boundary is fail-closed: authentication and authorization failures,
cross-session CSRF tokens, unrecognised upload signatures, unavailable virus
scanning, and exceeded request limits are refused and are recorded where an
audit writer is configured.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the organisation's
designated security contact. Include:

- the affected release and deployment mode;
- a concise description of the impact;
- reproducible steps or a minimal proof of concept;
- relevant request paths, timestamps in UTC, and sanitised logs; and
- any proposed mitigation, if available.

Do not include customer records, credentials, session cookies, CSRF tokens,
private keys, or other secrets in a report. Do not publicly disclose a report
until the security contact confirms that remediation and coordinated
disclosure are complete.

The security contact acknowledges a report within five business days, provides
an assessment when the impact is understood, and records the remediation or
accepted risk with an owner. Suspected active compromise should use the
organisation's incident-response channel immediately as well as this report
path.

## Supported versions

The latest released minor version receives security fixes. The preceding
released minor version receives fixes for critical vulnerabilities while an
upgrade is arranged. Older versions are unsupported and must be upgraded
before security assurance is claimed. A deployment must run the release's
database migration and security checks before it is placed into service.

Security advisories identify the affected versions, severity, exposure,
remediation, and any required configuration change. A fix is not considered
complete until its regression test and the relevant quality-gate evidence are
available.
