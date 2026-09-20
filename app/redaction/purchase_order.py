"""Configurable label-anchored PO fields; a heuristic, not general entity recognition."""

from .labeled_fields import detect_fields

DEFAULT_FIELDS = {
    "ship_to": {
        "labels": ["Ship To", "Deliver To", "Delivery Address"],
        "max_lines": 5,
    },
    "bill_to": {"labels": ["Bill To", "Billing Address"], "max_lines": 5},
    "buyer_name": {
        "labels": ["Buyer", "Buyer Name", "Ordered By", "Contact Name"],
        "max_lines": 1,
    },
    "supplier_name": {"labels": ["Supplier Name", "Vendor Name"], "max_lines": 1},
}
DEFAULT_STOPS = [
    "Purchase Order",
    "PO Number",
    "Order Date",
    "Date",
    "Item",
    "Description",
    "Quantity",
    "Qty",
    "Price",
    "Total",
    "Terms",
    "Payment Terms",
    "Notes",
    "Email",
    "Phone",
]


def detect(document, config):
    """Select configured PO values while retaining standard field boundaries."""
    fields = config.get("fields", DEFAULT_FIELDS)
    stops = config.get("stop_labels", DEFAULT_STOPS)
    if not isinstance(stops, list) or not all(isinstance(s, str) and s for s in stops):
        raise ValueError("stop_labels must be a list of non-empty strings.")
    stops = stops + [
        label for rule in DEFAULT_FIELDS.values() for label in rule["labels"]
    ]
    yield from detect_fields(document, fields, stops)
