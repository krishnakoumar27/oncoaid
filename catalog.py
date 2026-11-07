"""Catalog labels supplied by the project. Staff verify requirements before activation."""
NATIONAL = [
    "PM-JAY", "Health Minister's Cancer Patient Fund (HMCPF) under RAN",
    "Health Minister's Discretionary Grant (HMDG)", "Prime Minister's National Relief Fund (PMNRF)",
]
REGIONAL = {
    "CMCHIS": "Tamil Nadu", "MJPJAY": "Maharashtra", "Dr. YSR Aarogyasri": "Andhra Pradesh",
    "Rajiv Aarogyasri": "Telangana", "Ayushman Bharat - Arogya Karnataka": "Karnataka",
    "Mukh Mantri Punjab Cancer Raahat Kosh": "Punjab", "Swasthya Sathi": "West Bengal",
    "Delhi Arogya Kosh": "Delhi",
}
STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala",
    "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland",
    "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
]
ORGANIZATIONS = [
    "Tata Trusts", "Indian Cancer Society (ICS Cancer Cure Fund)",
    "Cancer Patients Aid Association (CPAA)", "St. Jude India ChildCare Centers", "CanKids...KidsCan",
]
CATALOG = (
    [(name, "central", "") for name in NATIONAL]
    + [(name, "state", state) for name, state in REGIONAL.items()]
    + [(f"Chief Minister's Relief Fund — {state}", "state", state) for state in STATES]
    + [(name, "organization", "") for name in ORGANIZATIONS]
)
