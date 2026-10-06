"""Catalog of the fields docfill knows.

Each field has a kind (which drives validation and normalisation), a group (how the wizard
arranges it) and label synonyms. The label-based extractor picks up synonyms automatically and
standard documents reference fields as ``{{ name }}``. Synonyms are matched case- and
accent-insensitively ("Județ" == "judet"); multilingual labels printed on identity cards such as
``Nume/Nom/Last name`` match through any of their parts.

Several people can be involved (shareholders, beneficial owners, board members): person 1 is the
applicant and uses the plain fields (``last_name``...); persons 2 and 3 use the same fields
prefixed ``p2_`` / ``p3_``. Each uploaded identity card fills one person, or the representative
(avocat / împuternicit) who files the request: their card fills ``filer_*`` and ``contact_*``.
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
    "person": "Persoana",
    "birth": "Nașterea",
    "domicile": "Domiciliul",
    "id_card": "Actul de identitate",
    "parents": "Părinții",
    "company": "Societatea",
    "capital": "Capitalul social, părțile sociale",
    "filing": "Cererea",
    "changes": "Înscriere de mențiuni",
    "closure": "Radiere",
    "contact": "Persoana pentru comunicare",
    "billing": "Date de facturare",
    "filer": "Cererea se depune de către",
    "roles": "Asociați / acționari, organe de conducere, beneficiari reali",
    "person2": "Persoana 2",
    "person3": "Persoana 3",
    "articles": "Actul constitutiv",
    "fiscal": "Înregistrarea fiscală (Anexa 1, vectorul fiscal)",
    "other": "Altele",
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


# A lawyer (împuternicire avocațială) or a proxy with a power of attorney (procură autentică):
# the options of ``representative_type``, see derive.py for what each one writes in the forms.
REPRESENTATIVE_TYPES = (
    "avocat (împuternicire avocațială)",
    "împuternicit (procură specială autentică)",
    "împuternicit (procură generală autentică)",
)


def _f(name, label, kind, group, synonyms=(), max_length=120, options=()) -> FieldSpec:
    return FieldSpec(name, label, kind, tuple(synonyms), group, max_length, tuple(options))


_SPECS = [
    # ---------------------------------------------------------------- person
    _f(
        "last_name",
        "Nume (de familie)",
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
        "Prenume",
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
        "Nume și prenume",
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
        "Cetățenia",
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
        "Data nașterii",
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
        "Locul nașterii",
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
        "Județul nașterii",
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
        "Țara nașterii",
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
        "Numele de familie al tatălui",
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
        "Prenumele tatălui",
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
        "Numele de familie al mamei",
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
        "Prenumele mamei",
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
        "Adresa de domiciliu",
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
        "Adresa (strada și numărul)",
        "address",
        "domicile",
        (
            "street address",
            "address line 1",
            "address line",
        ),
        150,
    ),
    _f("street", "Strada", "place", "domicile", ("street", "strada"), 100),
    _f(
        "street_number",
        "Numărul (nr.)",
        "short",
        "domicile",
        (
            "street number",
            "house number",
            "numar strada",
        ),
        12,
    ),
    _f("building", "Blocul", "short", "domicile", ("bloc", "building", "block"), 12),
    _f("entrance", "Scara", "short", "domicile", ("scara", "staircase"), 12),
    _f("floor", "Etajul", "short", "domicile", ("etaj", "floor"), 12),
    _f(
        "apartment",
        "Apartamentul (ap.)",
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
        "Localitatea",
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
        "Codul poștal",
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
        "Județul / sectorul",
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
        "Țara",
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
        "Tipul actului de identitate",
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
    _f(
        "id_series",
        "Seria actului de identitate",
        "id_series",
        "id_card",
        ("seria", "serie", "series"),
        4,
    ),
    _f(
        "id_number",
        "Numărul actului de identitate",
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
        "Actul de identitate emis de",
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
        "Data eliberării actului de identitate",
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
        "Actul de identitate valabil până la",
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
        "Denumirea firmei",
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
        "Sediul social (adresa completă)",
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
    _f("company_city", "Sediul social: localitatea", "place", "company", (), 80),
    _f("company_street", "Sediul social: strada", "place", "company", (), 100),
    _f("company_street_number", "Sediul social: numărul", "short", "company", (), 12),
    _f("company_building", "Sediul social: blocul", "short", "company", (), 12),
    _f("company_entrance", "Sediul social: scara", "short", "company", (), 12),
    _f("company_floor", "Sediul social: etajul", "short", "company", (), 12),
    _f("company_apartment", "Sediul social: apartamentul / camera", "short", "company", (), 20),
    _f("company_county", "Sediul social: județul / sectorul", "place", "company", (), 60),
    _f("company_email", "E-mailul societății", "email", "company", (), 120),
    _f(
        "company_euid",
        "EUID (identificatorul unic la nivel european)",
        "short",
        "company",
        ("euid",),
        40,
    ),
    _f("company_phone", "Telefonul societății", "phone", "company", (), 30),
    _f("company_website", "Pagina de internet a societății", "text", "company", (), 120),
    _f(
        "company_registration_number",
        "Numărul de ordine în registrul comerțului",
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
        "CUI (codul unic de înregistrare)",
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
    # The activities of the company (its object of activity). Anexa 4 writes them at third
    # parties (3.2), or at the registered office (3.1) when they are carried out there.
    _f(
        "caen_activities",
        "Activitățile CAEN ale societății (câte una pe rând: codul și denumirea, întâi "
        "activitatea principală)",
        "list",
        "company",
        (),
        4000,
    ),
    _f(
        "activities_at_office",
        "Activitățile se desfășoară la sediul social: Anexa 4 le trece la pct. 3.1 "
        "(nebifat: la pct. 3.2, activități desfășurate la terți)",
        "checkbox",
        "company",
    ),
    _f(
        "caen_third_party",
        "Alte activități CAEN desfășurate la terți (câte una pe rând: codul și denumirea)",
        "list",
        "company",
        (),
        4000,
    ),
    # Used by the legal checks of the procedures (see docfill.knowledge).
    _f(
        "share_capital",
        "Capitalul social (lei)",
        "short",
        "capital",
        ("capital social", "capitalul social", "capital social subscris"),
        30,
    ),
    _f(
        "associates",
        "Asociați / acționari (câte unul pe rând: nume | CNP/CUI | cotă de participare)",
        "list",
        "company",
        (),
        4000,
    ),
    _f(
        "family_members",
        "Membrii întreprinderii familiale (câte unul pe rând: nume | CNP)",
        "list",
        "company",
        (),
        2000,
    ),
    # ---------------------------------------------------------------- request / filing
    _f(
        "orc_office",
        "Oficiul registrului comerțului de pe lângă tribunalul",
        "place",
        "filing",
        ("oficiul registrului comertului de pe langa tribunalul",),
        60,
    ),
    _f("request_registration", "Cerere: înmatriculare", "checkbox", "filing"),
    _f("request_mentions", "Cerere: înscriere de mențiuni", "checkbox", "filing"),
    _f("request_closure", "Cerere: radiere", "checkbox", "filing"),
    _f("request_object", "Obiectul cererii", "text", "filing", ("obiectul cererii",), 150),
    _f("request_communication", "Cerere: comunicarea documentelor", "checkbox", "filing"),
    _f(
        "communication_method",
        "Comunicarea prin (poștă / curier / mijloace electronice / la sediul O.R.C.T.)",
        "text",
        "filing",
        (),
        40,
    ),
    _f(
        "capacity",
        "Semnează în calitate de",
        "text",
        "filing",
        (
            "in calitate de",
            "calitate",
        ),
        120,
    ),
    _f("represented_by", "Prin (reprezentantul)", "text", "filing", (), 120),
    _f("representation_basis", "Conform (temeiul reprezentării)", "text", "filing", (), 150),
    _f(
        "marital_regime",
        "Regimul matrimonial",
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
        "Cerere 4.3: declarație pe propria răspundere privind îndeplinirea condițiilor de "
        "funcționare (Anexa 4)",
        "checkbox",
        "filing",
    ),
    _f(
        "annex_numbers",
        "Anexele care fac parte integrantă din cerere (de exemplu, 4)",
        "short",
        "filing",
        (),
        30,
    ),
    _f(
        "attached_documents",
        "Documentele depuse (câte unul pe rând: denumirea | numărul/data | numărul de pagini)",
        "list",
        "filing",
        (),
        4000,
    ),
    _f("attached_pages_total", "Numărul total de pagini depuse", "short", "filing", (), 10),
    _f(
        "requested_changes",
        "Mențiunile solicitate (câte una pe rând)",
        "list",
        "changes",
        (),
        4000,
    ),
    _f("closure_basis", "Temeiul radierii", "text", "closure", (), 200),
    # ---------------------------------------------------------------- changes (Anexa 2a, 4.1-4.2)
    _f("change_name", "Modificare denumire firmă", "checkbox", "changes"),
    _f("change_legal_form", "Schimbare formă juridică", "checkbox", "changes"),
    _f("change_seat_county", "Schimbare sediu social în alt județ", "checkbox", "changes"),
    _f("new_seat_county", "Noul județ al sediului social", "place", "changes", (), 60),
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
    _f(
        "incoming_associates",
        "Numărul asociaților care intră în societate",
        "short",
        "changes",
        (),
        6,
    ),
    _f(
        "change_management",
        "Schimbare membri organe de conducere / administrare / control",
        "checkbox",
        "changes",
    ),
    _f("management_persons", "Numărul persoanelor numite", "short", "changes", (), 6),
    _f("associate_exit", "Asociat / acționar: excludere / retragere", "checkbox", "changes"),
    _f("change_capital", "Capital social (majorare / reducere)", "checkbox", "changes"),
    _f(
        "capital_change",
        "Modificarea capitalului social (majorare / reducere)",
        "text",
        "changes",
        (),
        20,
    ),
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
    _f("other_mentions_text", "Alte mențiuni (text)", "text", "changes", (), 200),
    _f("filed_capital_proof", "Depunere: dovadă vărsământ capital social", "checkbox", "changes"),
    _f(
        "filed_gm_decision",
        "Depunere: hotărâre AGA / decizie C.A. / decizie directorat",
        "checkbox",
        "changes",
    ),
    _f("filed_other", "Depunere: alte înscrisuri", "checkbox", "changes"),
    _f("filed_other_text", "Alte înscrisuri depuse (text)", "text", "changes", (), 200),
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
    # Who the representative filing the request is: asked each time, never remembered (it
    # decides "prin ... conform ..." of IV and "în calitate de ... conform ..." of XII).
    _f(
        "representative_type",
        "Reprezentantul care depune cererea este",
        "text",
        "filer",
        (),
        80,
        REPRESENTATIVE_TYPES,
    ),
    _f("filer_last_name", "Depune cererea: numele", "name", "filer", (), 80),
    _f("filer_first_name", "Depune cererea: prenumele", "name", "filer", (), 80),
    _f("filer_id_type", "Depune cererea: tipul actului de identitate", "short", "filer", (), 20),
    _f(
        "filer_id_series",
        "Depune cererea: seria actului de identitate",
        "id_series",
        "filer",
        (),
        4,
    ),
    _f(
        "filer_id_number",
        "Depune cererea: numărul actului de identitate",
        "id_number",
        "filer",
        (),
        12,
    ),
    _f("filer_cnp", "Depune cererea: CNP", "cnp", "filer", (), 20),
    _f(
        "filer_capacity",
        "Depune cererea: în calitate de (de exemplu, avocat)",
        "text",
        "filer",
        (),
        80,
    ),
    _f("filer_basis", "Depune cererea: conform (temeiul)", "text", "filer", (), 120),
    _f("filer_basis_number", "Depune cererea: numărul documentului", "short", "filer", (), 30),
    _f("filer_basis_date", "Depune cererea: data documentului", "date", "filer", (), 30),
    # ---------------------------------------------------------------- contact person (VII)
    _f("contact_last_name", "Persoana pentru comunicare: numele", "name", "contact", (), 80),
    _f("contact_first_name", "Persoana pentru comunicare: prenumele", "name", "contact", (), 80),
    _f("contact_city", "Persoana pentru comunicare: localitatea", "place", "contact", (), 80),
    _f("contact_street", "Persoana pentru comunicare: strada", "place", "contact", (), 100),
    _f("contact_street_number", "Persoana pentru comunicare: numărul", "short", "contact", (), 12),
    _f("contact_building", "Persoana pentru comunicare: blocul", "short", "contact", (), 12),
    _f("contact_entrance", "Persoana pentru comunicare: scara", "short", "contact", (), 12),
    _f("contact_floor", "Persoana pentru comunicare: etajul", "short", "contact", (), 12),
    _f("contact_apartment", "Persoana pentru comunicare: apartamentul", "short", "contact", (), 12),
    _f(
        "contact_county",
        "Persoana pentru comunicare: județul / sectorul",
        "place",
        "contact",
        (),
        60,
    ),
    _f(
        "contact_postal_code",
        "Persoana pentru comunicare: codul poștal",
        "postal_code",
        "contact",
        (),
        12,
    ),
    _f("contact_phone", "Persoana pentru comunicare: telefonul", "phone", "contact", (), 30),
    _f("contact_email", "Persoana pentru comunicare: e-mailul", "email", "contact", (), 120),
    # ---------------------------------------------------------------- billing (VIII)
    _f("billing_name", "Facturare: numele / denumirea", "text", "billing", (), 150),
    _f("billing_code", "Facturare: CNP / CUI", "short", "billing", (), 20),
    _f("billing_email", "Facturare: e-mailul", "email", "billing", (), 120),
    _f("billing_bank", "Facturare: banca", "text", "billing", (), 80),
    _f("billing_iban", "Facturare: IBAN", "iban", "billing", (), 40),
    _f("billing_address", "Facturare: sediul / adresa", "address", "billing", (), 250),
    # ---------------------------------------------------------------- general contact
    _f("email", "E-mail", "email", "person", ("e-mail", "email", "adresa de e-mail"), 120),
    # ---------------------------------------------------------------- independent activity (PFI)
    _f("profession", "Profesia / activitatea independentă", "text", "person", (), 120),
    _f(
        "profession_document",
        "Dreptul de exercitare a profesiei: documentul, numărul, data, emitentul",
        "text",
        "person",
        (),
        200,
    ),
    _f("phone", "Telefon", "phone", "person", ("telefon", "tel", "phone", "mobile"), 30),
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
    _f("associate", "asociat / acționar", "checkbox", "roles"),
    _f("shares", "părți sociale / acțiuni subscrise (număr)", "short", "roles", (), 15),
    _f("board_role", "funcția în conducerea societății", "text", "roles", (), 40, BOARD_ROLES),
    _f("general_director", "numit director general", "checkbox", "roles"),
    _f(
        "beneficial_owner",
        "beneficiar real: modul de exercitare a controlului (necompletat = nu este "
        "beneficiar real)",
        "text",
        "roles",
        (),
        120,
        CONTROL_OPTIONS,
    ),
    _f("control_description", "beneficiar real: descrierea controlului", "text", "roles"),
]

# ---------------------------------------------------------------- articles of incorporation (SA)
_SPECS += [
    *_ROLE_SPECS,
    _f("share_count", "Numărul de părți sociale / acțiuni", "short", "capital", (), 15),
    _f("share_form", "Acțiunile sunt", "text", "capital", (), 40, ("nominative", "la purtător")),
    _f(
        "company_duration",
        "Durata de funcționare, în ani (necompletat: nedeterminată)",
        "short",
        "articles",
        (),
        10,
    ),
    _f(
        "name_reservation_number",
        "Dovada disponibilității denumirii firmei: numărul",
        "short",
        "articles",
        (),
        30,
    ),
    _f(
        "name_reservation_date",
        "Dovada disponibilității denumirii firmei: data",
        "date",
        "articles",
        (),
        30,
    ),
    _f(
        "activity_object",
        "Obiectul de activitate (propus: activitatea CAEN principală)",
        "text",
        "articles",
        (),
        200,
    ),
    _f(
        "main_activity_domain",
        "Domeniul principal de activitate (denumirea grupei CAEN)",
        "text",
        "articles",
        (),
        150,
    ),
    _f(
        "administration",
        "Societatea este administrată de",
        "text",
        "articles",
        (),
        40,
        ("consiliu de administrație", "administrator unic"),
    ),
    _f("board_term_years", "Durata mandatului administratorilor (ani)", "short", "articles"),
    _f(
        "control_body",
        "Controlul financiar este exercitat de",
        "text",
        "articles",
        (),
        40,
        ("cenzori", "auditor financiar"),
    ),
    _f(
        "control_members",
        "Cenzorii / auditorul financiar (câte unul pe rând: datele de identificare)",
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
        "Înregistrare ca",
        "text",
        "fiscal",
        (),
        40,
        ("persoană juridică", "persoană fizică"),
    ),
    _f("profit_tax", "1. Impozit pe profit", "checkbox", "fiscal"),
    _f(
        "profit_tax_start",
        "1.1 Impozit pe profit datorat începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f(
        "profit_tax_period",
        "1.2 Perioada fiscală pentru impozitul pe profit",
        "text",
        "fiscal",
        (),
        20,
        ("trimestrială", "anuală"),
    ),
    _f("micro_tax", "2. Impozit pe veniturile microîntreprinderilor", "checkbox", "fiscal"),
    _f(
        "micro_tax_start",
        "2.1 Impozit pe veniturile microîntreprinderilor datorat începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f(
        "payroll_taxes",
        "3. Impozit pe veniturile din salarii și contribuții sociale",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_up_to_3_employees",
        "3.1.1 Număr mediu de salariați estimat de până la 3 salariați exclusiv",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_revenue_under_100k",
        "3.1.2 Venit total estimat de până la 100.000 euro",
        "checkbox",
        "fiscal",
    ),
    _f(
        "payroll_period",
        "3.2 Perioada fiscală pentru impozitul pe salarii și contribuții",
        "text",
        "fiscal",
        (),
        20,
        ("lunară", "trimestrială"),
    ),
    _f("salary_tax", "3.3 Impozit pe veniturile din salarii", "checkbox", "fiscal"),
    _f(
        "salary_tax_start",
        "3.3.1 Impozit pe veniturile din salarii datorat începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f(
        "cas_employee",
        "3.4 Contribuție de asigurări sociale (datorată de angajat)",
        "checkbox",
        "fiscal",
    ),
    _f(
        "cas_employee_start",
        "3.4.1 CAS datorată începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f(
        "cass_employee",
        "3.5 Contribuție de asigurări sociale de sănătate (datorată de angajat)",
        "checkbox",
        "fiscal",
    ),
    _f(
        "cass_employee_start",
        "3.5.1 CASS datorată începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f(
        "cam_employer",
        "3.6 Contribuție asiguratorie pentru muncă (datorată de angajator)",
        "checkbox",
        "fiscal",
    ),
    _f(
        "cam_employer_start",
        "3.6.1 CAM datorată începând cu data de (zz.ll.aaaa)",
        "date",
        "fiscal",
        (),
        30,
    ),
    _f("vat", "4. Taxa pe valoarea adăugată", "checkbox", "fiscal"),
    _f(
        "estimated_turnover",
        "4.1 Cifra de afaceri estimată a se realiza (lei, cel mult 8 cifre)",
        "short",
        "fiscal",
        (),
        20,
    ),
    _f(
        "vat_registration",
        "Înregistrare în scopuri de TVA",
        "text",
        "fiscal",
        (),
        80,
        (
            "4.2 depășirea plafonului de scutire (art. 316 alin. (1) lit. a) pct. 1)",
            "4.3 prin opțiune (art. 316 alin. (1) lit. a) pct. 2)",
        ),
    ),
    _f(
        "vat_period",
        "4.4 Perioada fiscală TVA",
        "text",
        "fiscal",
        (),
        20,
        ("lunară", "trimestrială"),
    ),
    _f("vat_cash_accounting", "4.5 TVA la încasare", "checkbox", "fiscal"),
]

# The beneficial owner declaration (ONRC Formular nr. 3) is filed by the legal representative or
# by a proxy (împuternicit, the "Filed by" person).
_SPECS.append(
    _f(
        "bo_filed_by",
        "Declarația privind beneficiarii reali este depusă de",
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

# The representative (avocat / împuternicit) who files the request for the company: not one of
# the persons of the company. Their identity card fills "Filed by" (XII) and the contact person
# (VII) instead of a person.
REPRESENTATIVE = 0
REPRESENTATIVE_FIELDS: dict[str, tuple[str, ...]] = {
    "last_name": ("filer_last_name", "contact_last_name"),
    "first_name": ("filer_first_name", "contact_first_name"),
    "cnp": ("filer_cnp",),
    "id_type": ("filer_id_type",),
    "id_series": ("filer_id_series",),
    "id_number": ("filer_id_number",),
    "city": ("contact_city",),
    "street": ("contact_street",),
    "street_number": ("contact_street_number",),
    "building": ("contact_building",),
    "entrance": ("contact_entrance",),
    "floor": ("contact_floor",),
    "apartment": ("contact_apartment",),
    "region": ("contact_county",),
    "postal_code": ("contact_postal_code",),
    "phone": ("contact_phone",),
    "email": ("contact_email",),
}


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
        specs.append(
            FieldSpec(**{**spec.__dict__, "label": f"Persoana 1 (solicitantul): {spec.label}"})
        )
    for index, prefix in enumerate(PERSON_PREFIXES[1:], start=2):
        for name in PERSON_FIELDS:
            spec = by_name[name]
            specs.append(
                FieldSpec(
                    name=prefix + name,
                    label=f"Persoana {index}: {spec.label[0].lower()}{spec.label[1:]}"
                    if name in PERSON_ROLES
                    else f"Persoana {index}: {spec.label}",
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
