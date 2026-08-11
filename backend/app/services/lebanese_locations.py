"""Controlled Lebanese commercial locations used by business onboarding."""

LOCATION_HIERARCHY: dict[str, dict[str, tuple[str, ...]]] = {
    "Beirut": {"Beirut": ("Beirut",)},
    "Mount Lebanon": {
        "Baabda": ("Baabda", "Hazmieh"),
        "Aley": ("Aley", "Choueifat"),
        "Metn": ("Antelias", "Jdeideh", "Sin El Fil", "Dekwaneh", "Baouchrieh"),
        "Keserwan": ("Jounieh", "Zouk Mikael", "Kaslik"),
        "Chouf": ("Beiteddine", "Damour", "Deir El Qamar"),
    },
    "North": {
        "Tripoli": ("Tripoli", "Mina"),
        "Zgharta": ("Zgharta", "Ehden"),
        "Koura": ("Amioun",),
    },
    "Akkar": {"Akkar": ("Halba",)},
    "Bekaa": {
        "Zahle": ("Zahle", "Chtaura"),
        "West Bekaa": ("Jeb Jennine", "Qab Elias"),
    },
    "Baalbek-Hermel": {"Baalbek": ("Baalbek",), "Hermel": ("Hermel",)},
    "South": {
        "Saida": ("Saida", "Abra", "Ghaziyeh"),
        "Jezzine": ("Jezzine",),
    },
    "Nabatieh": {
        "Nabatieh": ("Nabatieh", "Kfar Roummane"),
        "Bint Jbeil": ("Bint Jbeil",),
        "Marjayoun": ("Marjayoun", "Khiam"),
    },
}


def is_valid_location(
    governorate: str | None, district: str | None, city: str | None
) -> bool:
    """Return whether all three selected values form an approved hierarchy."""
    if governorate is None or district is None or city is None:
        return False
    districts = LOCATION_HIERARCHY.get(governorate)
    return districts is not None and city in districts.get(district, ())
