"""Catalog of the fields docfill knows.

Each field has a kind (which drives validation and normalisation), a group (how the wizard
arranges it) and label synonyms. The label-based extractor picks up synonyms automatically and
standard documents reference fields as ``{{ name }}``. Synonyms are matched case- and
accent-insensitively ("Județ" == "judet"); multilingual labels printed on identity cards such as
``Nume/Nom/Last name`` match through any of their parts.

Several people can be involved (shareholders, beneficial owners, board members): person 1 is the
applicant and uses the plain fields (``last_name``...); persons 2 and 3 use the same fields
prefixed ``p2_`` / ``p3_``. Each uploaded identity card fills one person.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

FieldKind = Literal[
    "name",
    "address",
    "place",
    "postal_code",
    "country",
    "text",
    "cnp",
    "date",
    "sex",
    "email",
    "phone",
    "iban",
    "id_series",
    "id_number",
    "short",
    "code",
    "checkbox",
    "list",
]

GROUPS = {
    "person": "Person",
    "birth": "Birth",
    "domicile": "Domicile",
    "id_card": "Identity card",
    "parents": "Parents",
    "company": "Company",
    "filing": "Request",
    "changes": "Changes (mențiuni)",
    "closure": "Closure (radiere)",
    "contact": "Contact person",
    "billing": "Billing",
    "filer": "Filed by",
    "roles": "Shareholders, management, beneficial owners",
    "person2": "Person 2",
    "person3": "Person 3",
    "articles": "Articles of incorporation (act constitutiv)",
    "fiscal": "Tax registration (Anexa 1, vector fiscal)",
    "other": "Other",
}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: FieldKind
    synonyms: tuple[str, ...] = ()
    group: str = "other"
    max_length: int = 120
    # The values a person picks from (shown as a list in the wizard).
    options: tuple[str, ...] = ()


def _f(name, label, kind, group, synonyms=(), max_length=120, options=()) -> FieldSpec:
    return FieldSpec(name, label, kind, tuple(synonyms), group, max_length, tuple(options))


_SPECS = [
    # ---------------------------------------------------------------- person
    _f(
        "last_name",
        "Last name (Nume)",
        "name",
        "person",
        (
            "last name",
            "surname",
            "family name",
            "nom",
            "nume",
            "numele",
            "nume de familie",
            "numele de familie",
        ),
        80,
    ),
    _f(
        "first_name",
        "First name (Prenume)",
        "name",
        "person",
        (
            "first name",
            "first names",
            "given name",
            "given names",
            "forename",
            "forenames",
            "christian name",
            "prenom",
            "prenume",
            "prenumele",
        ),
        80,
    ),
    _f(
        "full_name",
        "Full name",
        "name",
        "person",
        (
            "full name",
            "name",
            "name and surname",
            "applicant",
            "applicant name",
            "holder",
            "holder name",
            "nume si prenume",
            "numele si prenumele",
            "nume complet",
            "subsemnatul",
            "subsemnata",
            "subsemnatul(a)",
        ),
    ),
    _f(
        "cnp",
        "CNP",
        "cnp",
        "person",
        (
            "cnp",
            "cnp/nif",
            "cod numeric personal",
            "codul numeric personal",
            "personal numeric code",
            "personal code",
        ),
        20,
    ),
    _f("sex", "Sex", "sex", "person", ("sex", "sexul", "sexe", "gender"), 12),
    _f(
        "citizenship",
        "Citizenship (Cetățenia)",
        "text",
        "person",
        (
            "cetatenia",
            "cetatenie",
            "nationality",
            "nationalite",
            "citizenship",
        ),
        40,
    ),
    # ---------------------------------------------------------------- birth
    _f(
        "date_of_birth",
        "Date of birth",
        "date",
        "birth",
        (
            "date of birth",
            "birth date",
            "data nasterii",
            "data de nastere",
            "nascut la data",
            "date de naissance",
        ),
        30,
    ),
    _f(
        "place_of_birth",
        "Place of birth",
        "place",
        "birth",
        (
            "place of birth",
            "birthplace",
            "birth place",
            "city of birth",
            "locul nasterii",
            "loc nastere",
            "locul de nastere",
            "lieu de naissance",
            "nascut in localitatea",
            "nascut(a) in localitatea",
        ),
    ),
    _f(
        "birth_county",
        "County of birth",
        "place",
        "birth",
        (
            "judetul nasterii",
            "judet nastere",
        ),
        60,
    ),
    _f(
        "birth_country",
        "Country of birth",
        "country",
        "birth",
        (
            "tara nasterii",
            "country of birth",
        ),
        60,
    ),
    # ---------------------------------------------------------------- parents
    _f(
        "father_last_name",
        "Father's last name",
        "name",
        "parents",
        (
            "numele de familie al tatalui",
            "nume tata",
        ),
        80,
    ),
    _f(
        "father_first_name",
        "Father's first name",
        "name",
        "parents",
        (
            "prenumele tatalui",
            "prenume tata",
        ),
        80,
    ),
    _f(
        "mother_last_name",
        "Mother's last name",
        "name",
        "parents",
        (
            "numele de familie al mamei",
            "nume mama",
        ),
        80,
    ),
    _f(
        "mother_first_name",
        "Mother's first name",
        "name",
        "parents",
        (
            "prenumele mamei",
            "prenume mama",
        ),
        80,
    ),
    # ---------------------------------------------------------------- domicile
    _f(
        "full_address",
        "Address",
        "address",
        "domicile",
        (
            "address",
            "home address",
            "residential address",
            "permanent address",
            "address of residence",
            "place of residence",
            "residence",
            "domicile",
            "adresse",
            "adresa",
            "adresa de domiciliu",
            "domiciliu",
            "domiciliul",
            "domiciliat in",
            "domiciliata in",
            "resedinta",
        ),
        250,
    ),
    _f(
        "street_address",
        "Street line",
        "address",
        "domicile",
        (
            "street address",
            "address line 1",
            "address line",
        ),
        150,
    ),
    _f("street", "Street (Strada)", "place", "domicile", ("street", "strada"), 100),
    _f(
        "street_number",
        "Number (Nr.)",
        "short",
        "domicile",
        (
            "street number",
            "house number",
            "numar strada",
        ),
        12,
    ),
    _f("building", "Block (Bloc)", "short", "domicile", ("bloc", "building", "block"), 12),
    _f("entrance", "Staircase (Scara)", "short", "domicile", ("scara", "staircase"), 12),
    _f("floor", "Floor (Etaj)", "short", "domicile", ("etaj", "floor"), 12),
    _f(
        "apartment",
        "Apartment (Ap.)",
        "short",
        "domicile",
        (
            "apartament",
            "apartment",
            "flat",
        ),
        12,
    ),
    _f(
        "city",
        "City / locality",
        "place",
        "domicile",
        (
            "city",
            "town",
            "city/town",
            "locality",
            "municipality",
            "village",
            "oras",
            "orasul",
            "localitate",
            "localitatea",
            "municipiul",
            "comuna",
            "domiciliat in localitatea",
            "domiciliat(a) in localitatea",
        ),
        80,
    ),
    _f(
        "postal_code",
        "Postal code",
        "postal_code",
        "domicile",
        (
            "postal code",
            "postcode",
            "post code",
            "zip",
            "zip code",
            "zipcode",
            "cod postal",
        ),
        12,
    ),
    _f(
        "region",
        "County / sector (Județ)",
        "place",
        "domicile",
        (
            "state",
            "county",
            "province",
            "region",
            "state/province",
            "judet",
            "judetul",
            "judet/sector",
            "sector",
        ),
        80,
    ),
    _f(
        "country",
        "Country (Țara)",
        "country",
        "domicile",
        (
            "country",
            "country of residence",
            "tara",
            "tara de resedinta",
        ),
        80,
    ),
    # ---------------------------------------------------------------- identity card
    _f(
        "id_type",
        "ID document type",
        "short",
        "id_card",
        (
            "act identitate",
            "act de identitate",
            "tip act",
            "tipul actului",
            "document type",
        ),
        20,
    ),
    _f("id_series", "ID series (Seria)", "id_series", "id_card", ("seria", "serie", "series"), 4),
    _f(
        "id_number",
        "ID number (Nr.)",
        "id_number",
        "id_card",
        (
            "numar act",
            "nr act",
            "document number",
            "numarul actului",
        ),
        12,
    ),
    _f(
        "id_issued_by",
        "ID issued by",
        "text",
        "id_card",
        (
            "emis de",
            "emisa de",
            "emis(a) de",
            "eliberat de",
            "eliberata de",
            "issued by",
            "delivree par",
            "emisa de/delivree par/issued by",
        ),
        80,
    ),
    _f(
        "id_issue_date",
        "ID issue date",
        "date",
        "id_card",
        (
            "data eliberarii",
            "eliberat la data",
            "emis la data",
            "date of issue",
            "issue date",
        ),
        30,
    ),
    _f(
        "id_expiry_date",
        "ID valid until",
        "date",
        "id_card",
        (
            "valabil pana la",
            "valabil pana la data",
            "date of expiry",
            "expiry date",
        ),
        30,
    ),
    # ---------------------------------------------------------------- company
    _f(
        "company_name",
        "Company name",
        "text",
        "company",
        (
            "denumire firma",
            "denumirea firmei",
            "pentru firma",
            "company name",
        ),
        150,
    ),
    _f(
        "company_address",
        "Registered office (full)",
        "address",
        "company",
        (
            "sediul social",
            "sediu social",
            "sediul social in",
            "registered office",
        ),
        250,
    ),
    _f("company_city", "Registered office: locality", "place", "company", (), 80),
    _f("company_street", "Registered office: street", "place", "company", (), 100),
    _f("company_street_number", "Registered office: number", "short", "company", (), 12),
    _f("company_building", "Registered office: block", "short", "company", (), 12),
    _f("company_entrance", "Registered office: staircase", "short", "company", (), 12),
    _f("company_floor", "Registered office: floor", "short", "company", (), 12),
    _f("company_apartment", "Registered office: apartment / room", "short", "company", (), 20),
    _f("company_county", "Registered office: county / sector", "place", "company", (), 60),
    _f("company_email", "Company e-mail", "email", "company", (), 120),
    _f("company_euid", "EUID (European unique identifier)", "short", "company", ("euid",), 40),
    _f("company_phone", "Company phone", "phone", "company", (), 30),
    _f("company_website", "Company website", "text", "company", (), 120),
    _f(
        "company_registration_number",
        "Trade register number",
        "short",
        "company",
        (
            "numar de ordine in registrul comertului",
            "nr. de ordine",
            "registration number",
        ),
        30,
    ),
    _f(
        "company_cui",
        "CUI (fiscal code)",
        "short",
        "company",
        (
            "cui",
            "cod unic de inregistrare",
            "cif",
            "cod fiscal",
            "vat number",
        ),
        20,
    ),
    _f(
        "caen_activities",
        "CAEN activities at the registered office (one per line: code name)",
        "list",
        "company",
        (),
        4000,
    ),
    _f(
        "caen_third_party",
        "CAEN activities at third parties (one per line: code name)",
        "list",
        "company",
        (),
        4000,
    ),
    # Used by the legal checks of the procedures (see docfill.knowledge).
    _f(
        "share_capital",
        "Share capital (capital social, lei)",
        "short",
        "company",
        ("capital social", "capitalul social", "capital social subscris"),
        30,
    ),
    _f(
        "associates",
        "Associates / shareholders (one per line: name | CNP/CUI | share)",
        "list",
        "company",
        (),
        4000,
    ),
    _f(
        "family_members",
        "Family members of the IF (one per line: name | CNP)",
        "list",
        "company",
        (),
        2000,
    ),
    # ---------------------------------------------------------------- request / filing
    _f(
        "orc_office",
        "Trade register office (tribunal)",
        "place",
        "filing",
        ("oficiul registrului comertului de pe langa tribunalul",),
        60,
    ),
    _f("request_registration", "Request: înmatriculare (registration)", "checkbox", "filing"),
    _f("request_mentions", "Request: înscriere mențiuni (changes)", "checkbox", "filing"),
    _f("request_closure", "Request: radiere (closing)", "checkbox", "filing"),
    _f("request_object", "Subject of the request", "text", "filing", ("obiectul cererii",), 150),
    _f("request_communication", "Request: communication of the documents", "checkbox", "filing"),
    _f(
        "communication_method",
        "Communication by (poștă / curier / mijloace electronice / sediul O.R.C.T.)",
        "text",
        "filing",
        (),
        40,
    ),
    _f(
        "capacity",
        "Signs in the capacity of",
        "text",
        "filing",
        (
            "in calitate de",
            "calitate",
        ),
        120,
    ),
    _f("represented_by", "Represented by (prin)", "text", "filing", (), 120),
    _f("representation_basis", "Based on (conform)", "text", "filing", (), 150),
    _f(
        "marital_regime",
        "Marital regime",
        "text",
        "filing",
        (
            "regimul matrimonial",
            "regim matrimonial",
        ),
        80,
    ),
    _f(
        "request_operating_declaration",
        "Request 4.3: sworn statement on operating conditions (Anexa 4)",
        "checkbox",
        "filing",
    ),
    _f("annex_numbers", "Annexes that are part of the request (e.g. 4)", "short", "filing", (), 30),
    _f(
        "attached_documents",
        "Documents submitted (one per line: name | number/date | pages)",
        "list",
        "filing",
        (),
        4000,
    ),
    _f("attached_pages_total", "Total pages submitted", "short", "filing", (), 10),
    _f(
        "requested_changes",
        "Requested changes (mențiuni, one per line)",
        "list",
        "changes",
        (),
        4000,
    ),
    _f(
        "closure_basis", "Legal basis of the closure (temeiul radierii)", "text", "closure", (), 200
    ),
    # ---------------------------------------------------------------- changes (Anexa 2a, 4.1-4.2)
    _f("change_name", "Modificare denumire firmă", "checkbox", "changes"),
    _f("change_legal_form", "Schimbare formă juridică", "checkbox", "changes"),
    _f("change_seat_county", "Schimbare sediu social în alt județ", "checkbox", "changes"),
    _f("new_seat_county", "New county of the registered office", "place", "changes", (), 60),
    _f(
        "change_seat",
        "Sediu social (prelungire / modificare act de spațiu)",
        "checkbox",
        "changes",
    ),
    _f(
        "change_branches",
        "Sedii secundare (înființare / desființare / modificare)",
        "checkbox",
        "changes",
    ),
    _f("change_duration", "Durată de funcționare (prelungire / reducere)", "checkbox", "changes"),
    _f("suspend_activity", "Suspendare activitate", "checkbox", "changes"),
    _f("resume_activity", "Reluare activitate", "checkbox", "changes"),
    _f("change_activity", "Modificare obiect de activitate", "checkbox", "changes"),
    _f("transfer_shares", "Transmitere părți sociale / acțiuni", "checkbox", "changes"),
    _f("incoming_associates", "Number of associates joining", "short", "changes", (), 6),
    _f(
        "change_management",
        "Schimbare membri organe de conducere / administrare / control",
        "checkbox",
        "changes",
    ),
    _f("management_persons", "Number of persons appointed", "short", "changes", (), 6),
    _f("associate_exit", "Asociat / acționar: excludere / retragere", "checkbox", "changes"),
    _f("change_capital", "Capital social (majorare / reducere)", "checkbox", "changes"),
    _f("capital_change", "Capital change (majorare / reducere)", "text", "changes", (), 20),
    _f(
        "change_identification",
        "Modificare date de identificare ale profesionistului",
        "checkbox",
        "changes",
    ),
    _f("change_marital_regime", "Modificare regim matrimonial", "checkbox", "changes"),
    _f("mandate_resignation", "Renunțare mandat", "checkbox", "changes"),
    _f("dissolution", "Dizolvare", "checkbox", "changes"),
    _f("liquidator_appointment", "Numire lichidator", "checkbox", "changes"),
    _f("certificate_exchange", "Preschimbare certificat de înregistrare", "checkbox", "changes"),
    _f("updated_constitutive_act", "Depunere act constitutiv actualizat", "checkbox", "changes"),
    _f("other_mentions", "Alte mențiuni", "checkbox", "changes"),
    _f("other_mentions_text", "Other changes (text)", "text", "changes", (), 200),
    _f("filed_capital_proof", "Depunere: dovadă vărsământ capital social", "checkbox", "changes"),
    _f(
        "filed_gm_decision",
        "Depunere: hotărâre AGA / decizie C.A. / decizie directorat",
        "checkbox",
        "changes",
    ),
    _f("filed_other", "Depunere: alte înscrisuri", "checkbox", "changes"),
    _f("filed_other_text", "Other documents filed (text)", "text", "changes", (), 200),
    # ---------------------------------------------------------------- closure (Anexa 2a, 6)
    _f("closure_legal_person", "Radiere: persoană juridică", "checkbox", "closure"),
    _f("closure_by_will", "Motiv: voința profesionistului", "checkbox", "closure"),
    _f("closure_by_court", "Motiv: hotărâre judecătorească", "checkbox", "closure"),
    _f("closure_other_reason", "Motiv: altele (text)", "text", "closure", (), 200),
    _f(
        "closure_liquidation_report",
        "Mențiune: raport de repartizare a activului rămas după lichidare depus",
        "checkbox",
        "closure",
    ),
    _f(
        "filed_liquidation_statements",
        "Depunere: situații financiare finale de lichidare și repartizare",
        "checkbox",
        "closure",
    ),
    # ---------------------------------------------------------------- filed by (XII)
    _f("filer_last_name", "Filed by: last name", "name", "filer", (), 80),
    _f("filer_first_name", "Filed by: first name", "name", "filer", (), 80),
    _f("filer_id_type", "Filed by: ID document type", "short", "filer", (), 20),
    _f("filer_id_series", "Filed by: ID series", "id_series", "filer", (), 4),
    _f("filer_id_number", "Filed by: ID number", "id_number", "filer", (), 12),
    _f("filer_cnp", "Filed by: CNP", "cnp", "filer", (), 20),
    _f("filer_capacity", "Filed by: capacity (e.g. avocat)", "text", "filer", (), 80),
    _f("filer_basis", "Filed by: based on (conform)", "text", "filer", (), 120),
    _f("filer_basis_number", "Filed by: document number", "short", "filer", (), 30),
    _f("filer_basis_date", "Filed by: document date", "date", "filer", (), 30),
    # ---------------------------------------------------------------- contact person (VII)
    _f("contact_last_name", "Contact: last name", "name", "contact", (), 80),
    _f("contact_first_name", "Contact: first name", "name", "contact", (), 80),
    _f("contact_city", "Contact: locality", "place", "contact", (), 80),
    _f("contact_street", "Contact: street", "place", "contact", (), 100),
    _f("contact_street_number", "Contact: number", "short", "contact", (), 12),
    _f("contact_building", "Contact: block", "short", "contact", (), 12),
    _f("contact_entrance", "Contact: staircase", "short", "contact", (), 12),
    _f("contact_floor", "Contact: floor", "short", "contact", (), 12),
    _f("contact_apartment", "Contact: apartment", "short", "contact", (), 12),
    _f("contact_county", "Contact: county / sector", "place", "contact", (), 60),
    _f("contact_postal_code", "Contact: postal code", "postal_code", "contact", (), 12),
    _f("contact_phone", "Contact: phone", "phone", "contact", (), 30),
    _f("contact_email", "Contact: e-mail", "email", "contact", (), 120),
    # ---------------------------------------------------------------- billing (VIII)
    _f("billing_name", "Billing: name / company", "text", "billing", (), 150),
    _f("billing_code", "Billing: CNP / CUI", "short", "billing", (), 20),
    _f("billing_email", "Billing: e-mail", "email", "billing", (), 120),
    _f("billing_bank", "Billing: bank", "text", "billing", (), 80),
    _f("billing_iban", "Billing: IBAN", "iban", "billing", (), 40),
    _f("billing_address", "Billing: registered office / address", "address", "billing", (), 250),
    # ---------------------------------------------------------------- general contact
    _f("email", "E-mail", "email", "person", ("e-mail", "email", "adresa de e-mail"), 120),
    # ---------------------------------------------------------------- independent activity (PFI)
    _f("profession", "Profession / independent activity (profesia)", "text", "person", (), 120),
    _f(
        "profession_document",
        "Right to practise: document, number, date, issuer",
        "text",
        "person",
        (),
        200,
    ),
    _f("phone", "Phone", "phone", "person", ("telefon", "tel", "phone", "mobile"), 30),
]

# ---------------------------------------------------------------- roles of each person
# Legea nr. 129/2019, art. 4 alin. (2): how a beneficial owner controls the company (the boxes of
# the ONRC declaration). Only lit. a) concerns companies; the hints are short reminders.
CONTROL_OPTIONS = (
    "art. 4 alin. (2) lit. a) pct. 1 (deținere / control direct sau indirect, peste 25%)",
    "art. 4 alin. (2) lit. a) pct. 2 (funcție de conducere de nivel superior)",
    "art. 4 alin. (2) lit. d) pct. 1",
    "art. 4 alin. (2) lit. d) pct. 2",
    "art. 4 alin. (2) lit. d) pct. 3",
    "art. 4 alin. (2) lit. d) pct. 4",
)
BOARD_ROLES = ("președinte", "membru", "administrator unic", "administrator")

_ROLE_SPECS = [
    _f("shares", "shares subscribed (number of shares)", "short", "roles", (), 15),
    _f("board_role", "board role", "text", "roles", (), 40, BOARD_ROLES),
    _f("general_director", "appointed general director", "checkbox", "roles"),
    _f(
        "beneficial_owner",
        "beneficial owner: how control is exercised (empty = not a beneficial owner)",
        "text",
        "roles",
        (),
        120,
        CONTROL_OPTIONS,
    ),
    _f("control_description", "beneficial owner: description of the control", "text", "roles"),
]

# ---------------------------------------------------------------- articles of incorporation (SA)
_SPECS += [
    *_ROLE_SPECS,
    _f("share_count", "Number of shares (acțiuni)", "short", "articles", (), 15),
    _f("share_form", "Shares are", "text", "articles", (), 40, ("nominative", "la purtător")),
    _f(
        "company_duration",
        "Duration in years (empty: nedeterminată, undetermined)",
        "short",
        "articles",
        (),
        10,
    ),
    _f(
        "name_reservation_number",
        "Name availability proof (dovada disponibilității firmei): number",
        "short",
        "articles",
        (),
        30,
    ),
    _f("name_reservation_date", "Name availability proof: date", "date", "articles", (), 30),
    _f(
        "main_activity_domain",
        "Main field of activity (domeniul principal, name of the CAEN group)",
        "text",
        "articles",
        (),
        150,
    ),
    _f(
        "administration",
        "Administered by",
        "text",
        "articles",
        (),
        40,
        ("consiliu de administrație", "administrator unic"),
    ),
    _f("board_term_years", "Term of office of the administrators (years)", "short", "articles"),
    _f(
        "control_body",
        "Financial control by",
        "text",
        "articles",
        (),
        40,
        ("cenzori", "auditor financiar"),
    ),
    _f(
        "control_members",
        "Censors / financial auditor (one per line: identification)",
        "list",
        "articles",
        (),
        4000,
    ),
]

# ---------------------------------------------------------------- tax registration (Anexa 1)
_SPECS += [
    _f(
        "taxpayer_type",
        "Registered as",
        "text",
        "fiscal",
        (),
        40,
        ("persoană juridică", "persoană fizică"),
    ),
    _f("profit_tax", "1. Impozit pe profit", "checkbox", "fiscal"),
    _f("profit_tax_start", "1.1 Profit tax from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f(
        "profit_tax_period",
        "1.2 Profit tax period",
        "text",
        "fiscal",
        (),
        20,
        ("trimestrială", "anuală"),
    ),
    _f("micro_tax", "2. Impozit pe veniturile microîntreprinderilor", "checkbox", "fiscal"),
    _f("micro_tax_start", "2.1 Micro-enterprise tax from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f(
        "payroll_taxes",
        "3. Impozit pe veniturile din salarii și contribuții sociale",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_up_to_3_employees",
        "3.1.1 Up to 3 employees on average (estimated)",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_revenue_under_100k",
        "3.1.2 Total revenue up to 100.000 euro (estimated)",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_period",
        "3.2 Payroll tax period",
        "text",
        "fiscal",
        (),
        20,
        ("lunară", "trimestrială"),
    ),
    _f("salary_tax", "3.3 Impozit pe veniturile din salarii", "checkbox", "fiscal"),
    _f("salary_tax_start", "3.3.1 Salary tax from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f("cas_employee", "3.4 Contribuție de asigurări sociale (angajat)", "checkbox", "fiscal"),
    _f("cas_employee_start", "3.4.1 CAS from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f(
        "cass_employee",
        "3.5 Contribuție de asigurări sociale de sănătate (angajat)",
        "checkbox",
        "fiscal",
    ),
    _f("cass_employee_start", "3.5.1 CASS from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f("cam_employer", "3.6 Contribuție asiguratorie pentru muncă", "checkbox", "fiscal"),
    _f("cam_employer_start", "3.6.1 CAM from (dd.mm.yyyy)", "date", "fiscal", (), 30),
    _f("vat", "4. Taxa pe valoarea adăugată", "checkbox", "fiscal"),
    _f(
        "estimated_turnover",
        "4.1 Estimated turnover (lei, up to 8 digits)",
        "short",
        "fiscal",
        (),
        20,
    ),
    _f(
        "vat_registration",
        "VAT registration",
        "text",
        "fiscal",
        (),
        80,
        (
            "4.2 depășirea plafonului de scutire (art. 316 alin. (1) lit. a) pct. 1)",
            "4.3 prin opțiune (art. 316 alin. (1) lit. a) pct. 2)",
        ),
    ),
    _f("vat_period", "4.4 VAT period", "text", "fiscal", (), 20, ("lunară", "trimestrială")),
    _f("vat_cash_accounting", "4.5 TVA la încasare", "checkbox", "fiscal"),
]

# The beneficial owner declaration (ONRC Formular nr. 3) is filed by the legal representative or
# by a proxy (împuternicit, the "Filed by" person).
_SPECS.append(
    _f(
        "bo_filed_by",
        "Beneficial owner declaration filed by",
        "text",
        "filing",
        (),
        40,
        ("reprezentantul legal", "împuternicit"),
    )
)

# ---------------------------------------------------------------- persons 2 and 3
PERSON_PREFIXES = ("", "p2_", "p3_")
MAX_PERSONS = len(PERSON_PREFIXES)
# The fields that describe one person, repeated for every person (read from their identity card).
PERSON_BASE = (
    "last_name",
    "first_name",
    "full_name",
    "cnp",
    "sex",
    "citizenship",
    "date_of_birth",
    "place_of_birth",
    "birth_county",
    "birth_country",
    "full_address",
    "street_address",
    "street",
    "street_number",
    "building",
    "entrance",
    "floor",
    "apartment",
    "city",
    "postal_code",
    "region",
    "country",
    "id_type",
    "id_series",
    "id_number",
    "id_issued_by",
    "id_issue_date",
    "id_expiry_date",
)
PERSON_ROLES = tuple(spec.name for spec in _ROLE_SPECS)
PERSON_FIELDS = PERSON_BASE + PERSON_ROLES


def person_prefix(person: int) -> str:
    """``1`` -> ``""`` (the applicant), ``2`` -> ``"p2_"``..."""
    if not 1 <= person <= MAX_PERSONS:
        raise ValueError(f"person must be between 1 and {MAX_PERSONS}")
    return PERSON_PREFIXES[person - 1]


def split_person(name: str) -> tuple[int, str]:
    """``p2_cnp`` -> ``(2, "cnp")``; ``cnp`` -> ``(1, "cnp")``; other fields -> ``(0, name)``."""
    for index, prefix in enumerate(PERSON_PREFIXES[1:], start=2):
        if name.startswith(prefix) and name[len(prefix) :] in PERSON_FIELDS:
            return index, name[len(prefix) :]
    return (1, name) if name in PERSON_FIELDS else (0, name)


def base_field(name: str) -> str:
    """The field a person's field repeats: ``p2_cnp`` -> ``cnp``."""
    return split_person(name)[1]


def person_view(values: Mapping[str, str], person: int) -> dict[str, str]:
    """The values as seen by a document about one person: that person's fields under the plain
    names (``p2_cnp`` -> ``cnp``), the company and filing fields unchanged."""
    prefix = person_prefix(person)
    if not prefix:
        return dict(values)
    view = {name: value for name, value in values.items() if split_person(name)[0] == 0}
    for name in PERSON_FIELDS:
        if value := values.get(prefix + name):
            view[name] = value
    return view


def persons_with(values: Mapping[str, str], roles: Iterable[str]) -> list[int]:
    """The persons having any of ``roles`` (``["board_role"]``: the administrators); person 1
    when nobody has one."""
    roles = list(roles)
    found = [
        index
        for index, prefix in enumerate(PERSON_PREFIXES, start=1)
        if any((values.get(prefix + role) or "").strip() for role in roles)
    ]
    return found or [1]


def _person_specs() -> list[FieldSpec]:
    by_name = {spec.name: spec for spec in _SPECS}
    specs = []
    for name in PERSON_ROLES:  # person 1's roles
        spec = by_name[name]
        specs.append(FieldSpec(**{**spec.__dict__, "label": f"Person 1 (applicant): {spec.label}"}))
    for index, prefix in enumerate(PERSON_PREFIXES[1:], start=2):
        for name in PERSON_FIELDS:
            spec = by_name[name]
            specs.append(
                FieldSpec(
                    name=prefix + name,
                    label=f"Person {index}: {spec.label[0].lower()}{spec.label[1:]}"
                    if name in PERSON_ROLES
                    else f"Person {index}: {spec.label}",
                    kind=spec.kind,
                    synonyms=(),  # never read from labels: identity cards fill them
                    group="roles" if name in PERSON_ROLES else f"person{index}",
                    max_length=spec.max_length,
                    options=spec.options,
                )
            )
    return specs


_SPECS = [spec for spec in _SPECS if spec.name not in PERSON_ROLES] + _person_specs()


FIELDS: dict[str, FieldSpec] = {spec.name: spec for spec in _SPECS}

# Labels that only mean something on one kind of document (checked before general synonyms).
TYPE_SYNONYMS: dict[str, dict[str, str]] = {
    "birth_certificate": {
        "judetul": "birth_county",
        "comuna": "place_of_birth",
        "orasul": "place_of_birth",
        "municipiul": "place_of_birth",
        "comuna/orasul/municipiul": "place_of_birth",
        "orasul/municipiul": "place_of_birth",
        "sexul": "sex",
    },
    "id_card": {
        "loc nastere": "place_of_birth",
        "domiciliu": "full_address",
    },
}


def field_labels() -> dict[str, str]:
    return {name: spec.label for name, spec in FIELDS.items()}
