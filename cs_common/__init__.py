"""Code shared by the Django IDE service and the Agent Engine service.

Only small, dependency-free helpers live here (service signing, redaction,
data-directory resolution). Neither service imports the other's internals.
"""
