"""Independent verification sources: each one answers in the shape of a real service.

Free public sources are called live (the RBI/Razorpay IFSC directory). Sources that need a paid provider contract
(bank-account validation, "penny drop") run against a labelled SANDBOX that knows the demo's fictional samples, the
way the simulated GST registry does (pipeline/registry.py). Swapping in a provider means changing an adapter here, not
the pipeline: results are stored as verification records and judged by the checks like any other evidence.
"""
