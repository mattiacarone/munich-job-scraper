"""
Profile matching, gate filtering, and relevance scoring engine tailored for Mattia Carone.
Enforces the hard gates strictly in order:
  - Location (Munich + 23 suburbs -> 'munich', Germany/DACH remote -> 'remote-de', EU/EMEA remote -> 'remote-eu')
  - Seniority (Director, Head of, VP, Principal, Lead, Intern, or Senior/Sr./Staff/Lead with 5+ years)
  - Skip-list vs data/ger_skip_list.csv (scope: all) + aggregator/staffing blocklist
  - Off-function titles
  - Duplicate check vs applied_history.csv (exact, 14-char prefix match on Submitted, 3-sent company cap)
  - German language gate ('german' for German title/text; 'german_required' for English postings requiring German)
  - Foreign language gate ('language' for FR/SV/NL/ES/IT/PL text)
  - Years required gate ('years_required' for 6+ years, or 5+ years with Senior/Sr./Staff/Lead/Principal title)
  - Contract gate ('contract' for fixed_term, temporary, part_time, intern, freelance, parental cover)
  - Deep stack gate ('deep_stack' for central Kubernetes/Terraform/Linux admin, SAP ABAP, embedded C/C++, kernel, ML training, SOC/pentest, mainframe)
  - Clearance gate ('clearance' for SÜG/SUEG, Sicherheitsüberprüfung, SC/DV, German citizenship required)
  - Age gate ('age' > 60 days, liveness check for > 14 days)
  - Apply route & needs_account classification

Scoring applies only to postings that successfully pass all gates, incorporating outcome-based
title-keyword weights and role-family priors (Brief 9 Section 1) plus freshness (+8 for <=48h, +4 for <=7d).
"""

from collections import defaultdict
import csv
import html
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple
from apply_classifier import classify_apply_kind, classify_apply_route, extract_host_from_url
from config import ProfileConfig, get_applied_history_path, get_ger_skip_list_path, get_outcomes_path
import liveness
from intake_checks import check_clera, check_employer_link, check_hard_blocks, is_clera
from models import JobPosting

DATA_DIR = Path(__file__).resolve().parent / "data"

# Brief 13 Section 1: Role rules from application pipeline
ROLE_RULES_PRIMARY_PATH = Path(r"C:\Users\Mattia\Desktop\Job Application material\memory\role_rules.json")
ROLE_RULES_FALLBACK_PATHS = [
    Path(__file__).resolve().parent / "memory" / "role_rules.json",
    Path(__file__).resolve().parent / "data" / "role_rules.json",
]


def load_role_rules() -> Optional[dict]:
    """Load role rules JSON. Returns None if missing or invalid."""
    candidates = [ROLE_RULES_PRIMARY_PATH] + ROLE_RULES_FALLBACK_PATHS
    target_path = None
    for p in candidates:
        if p.exists():
            target_path = p
            break
    if not target_path:
        logger.warning(f"[Matcher] role_rules.json not found in any candidate path (checked {ROLE_RULES_PRIMARY_PATH})")
        return None
    try:
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        required_keys = {"family", "family_not", "seniority", "seniority_ok", "years_block", "german_title"}
        if not required_keys.issubset(data.keys()):
            logger.warning(f"[Matcher] role_rules.json missing required keys: {required_keys - set(data.keys())}")
            return None
        return data
    except Exception as exc:
        logger.warning(f"[Matcher] Failed to read or parse role_rules.json: {exc}")
        return None


VAGUE_LOCATION_TOKENS = {
    "remote", "hybrid", "germany", "deutschland", "de", "europe", "emea", "eu",
    "anywhere", "deutschlandweit", "bundesweit", "home office", "homeoffice", "flexible",
    "fully remote", "100% remote", "work from home", "wfh", "home-office",
}

OTHER_REGION_IN_TITLE_RE = re.compile(
    r"\b(latam|latin\s+america|us|usa|u\.s\.a?\.|north\s+america|americas|apac|"
    r"uk|u\.k\.|united\s+kingdom|france|spain|italy|nordics|benelux|netherlands|"
    r"poland|india|canada|japan|singapore|australia|mena)\b",
    re.I,
)


def is_vague_location(loc_str: Optional[str]) -> bool:
    """
    Brief 14 Section A2:
    After trimming and lowercasing, the location is empty, or it is one of
    remote, hybrid, germany, deutschland, de, europe, emea, eu, anywhere,
    deutschlandweit, bundesweit, home office, homeoffice, flexible,
    or a combination such as 'remote, germany' or 'germany (remote)'.
    """
    t = (loc_str or "").strip().lower()
    if not t or t in ("unknown", "n/a", "none", "null", "tbd"):
        return True
    if t in VAGUE_LOCATION_TOKENS:
        return True
    parts = [p.strip() for p in re.split(r"[,;/|\-–—\(\)\[\]]+", t) if p.strip()]
    if parts and all(p in VAGUE_LOCATION_TOKENS for p in parts):
        return True
    return False


MUNICH_SUBURBS = [
    "munich", "münchen", "garching", "unterföhring", "ismaning", "aschheim",
    "feldkirchen", "planegg", "martinsried", "oberhaching", "gilching",
    "hallbergmoos", "unterschleißheim", "poing", "grünwald", "pullach",
    "neubiberg", "haar", "freising", "erding", "ottobrunn", "taufkirchen",
    "germering", "dachau", "starnberg", "gräfelfing", "graefelfing",
    "krailling", "weßling", "wessling", "oberpfaffenhofen", "karlsfeld",
    "holzkirchen", "kirchheim", "heimstetten", "vaterstetten", "baldham",
    "fürstenfeldbruck", "fuerstenfeldbruck", "gauting", "unterhaching",
    "parsdorf", "manching", "brunnthal", "olching", "puchheim", "sauerlach",
    "ebersberg", "bergkirchen", "maisach"
]
# 6 Nov 2026: XING / germantechjobs / StepStone write umlauts as ASCII ("Muenchen", "Unterfoehring", "Gruenwald") or drop them
# ("Munchen"); 724 postings in 14 days were rejected as "unknown location" only because of the spelling.
MUNICH_SUBURBS += sorted({v for s in MUNICH_SUBURBS for v in (
    s.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss"),
    s.replace("ü", "u").replace("ö", "o").replace("ä", "a").replace("ß", "ss")) if v != s} - set(MUNICH_SUBURBS))

MUNICH_POSTAL_CODES_RE = re.compile(
    r"\b(?:d-|de-)?(80\d{3}|81\d{3}|820\d{2}|821\d{2}|822\d{2}|853\d{2}|855\d{2}|856\d{2}|857\d{2})\b",
    re.I,
)

OTHER_CITIES = [
    "berlin", "hamburg", "frankfurt", "stuttgart", "cologne", "köln",
    "düsseldorf", "leipzig", "dresden", "hannover", "nuremberg", "nürnberg",
    "bremen", "essen", "dortmund", "bonn", "karlsruhe", "mannheim", "augsburg",
    "freiburg", "finsterwalde", "heidelberg", "darmstadt", "mainz", "wiesbaden",
    "münster", "bielefeld", "aachen", "kiel", "lübeck", "rostock", "erfurt",
    "jena", "magdeburg", "potsdam", "ulm", "regensburg", "würzburg", "ingolstadt",
    "walldorf", "leverkusen", "wolfsburg", "braunschweig", "osnabrück", "oldenburg",
    "göttingen", "kassel", "saarbrücken", "koblenz", "paderborn", "heilbronn",
    "reutlingen", "tübingen", "konstanz", "friedrichshafen", "rosenheim", "landshut",
    "london", "paris", "amsterdam", "vienna", "wien", "zurich", "zürich",
    "madrid", "barcelona", "milan", "milano", "rome", "roma", "dublin", "warsaw",
    "warszawa", "prague", "praha", "montpellier", "lyon", "marseille", "toulouse",
    "nantes", "strasbourg", "bordeaux", "lille", "rennes", "reims", "grenoble",
    "dijon", "angers", "nîmes", "nimes", "clermont-ferrand", "tours", "amiens",
    "limoges", "annecy", "perpignan", "besançon", "besancon", "metz", "orléans",
    "orleans", "rouen", "mulhouse", "caen", "nancy", "saint-étienne", "saint-etienne",
    "roissy", "la rochelle", "cahors", "montbéliard", "montbeliard", "pontivy",
    "verdun", "pontarlier", "boulogne-billancourt", "courbevoie", "levallois-perret",
    "issy-les-moulineaux", "neuilly-sur-seine", "saint-denis", "nanterre", "versailles",
    "massy", "vélizy", "velizy", "rotterdam", "the hague", "den haag", "utrecht",
    "eindhoven", "tilburg", "groningen", "almere", "breda", "nijmegen", "arnhem",
    "haarlem", "enschede", "delft", "leiden", "maastricht", "stockholm", "gothenburg",
    "göteborg", "malmö", "malmo", "uppsala", "västerås", "vasteras", "örebro",
    "orebro", "linköping", "linkoping", "helsingborg", "jönköping", "jonkoping",
    "norrköping", "norrkoping", "lund", "umeå", "umea", "oslo", "bergen", "trondheim",
    "stavanger", "copenhagen", "københavn", "aarhus", "odense", "aalborg", "helsinki",
    "espoo", "tampere", "turku", "oulu", "krakow", "kraków", "wroclaw", "wrocław",
    "poznan", "poznań", "gdansk", "gdańsk", "szczecin", "bydgoszcz", "lublin",
    "katowice", "lodz", "łódź", "valencia", "seville", "sevilla", "zaragoza",
    "málaga", "malaga", "murcia", "palma", "bilbao", "alicante", "córdoba", "cordoba",
    "valladolid", "vigo", "gijón", "gijon", "turin", "torino", "naples", "napoli",
    "palermo", "genoa", "genova", "bologna", "florence", "firenze", "bari", "catania",
    "venice", "venezia", "verona", "padua", "padova", "trieste", "brescia", "parma",
    "modena", "manchester", "birmingham", "leeds", "glasgow", "sheffield", "bradford",
    "liverpool", "edinburgh", "bristol", "cardiff", "belfast", "nottingham",
    "southampton", "leicester", "coventry", "reading", "cambridge", "oxford",
    "brighton", "milton keynes", "geneva", "genève", "genf", "basel", "lausanne",
    "bern", "winterthur", "lucerne", "luzern", "st. gallen", "lugano", "zug",
    "linz", "graz", "salzburg", "innsbruck", "klagenfurt", "brussels", "bruxelles",
    "brüssel", "antwerp", "antwerpen", "ghent", "gent", "charleroi", "liège", "liege",
    "bruges", "brugge", "namur", "leuven", "lisbon", "lisboa", "porto", "braga",
    "coimbra", "budapest", "bucharest", "sofia", "athens", "zagreb", "belgrade",
    "ljubljana", "bratislava", "tallinn", "riga", "vilnius", "luxembourg", "jeddah",
    "riyadh", "dubai", "abu dhabi", "doha", "tel aviv", "cairo", "istanbul", "ankara",
    "tokyo", "seoul", "singapore", "sydney", "melbourne", "toronto", "vancouver",
    "montreal", "new york", "san francisco", "los angeles", "chicago", "boston",
    "seattle", "austin"
]

NON_GERMAN_COUNTRY_TLD_RE = re.compile(
    r"https?://[^/]+\.(?:fr|se|nl|es|it|pl|dk|no|be|co\.uk|uk|ch|at|pt|fi|cz|hu|ro|ie)(?:[/:?#]|$)",
    re.I,
)

FOREIGN_COUNTRIES_PAT = re.compile(
    r"\b("
    r"france|spain|españa|italy|italia|netherlands|nederland|belgium|belgië|belgique|"
    r"poland|polska|sweden|sverige|norway|norge|denmark|danmark|finland|suomi|"
    r"portugal|greece|austria|österreich|switzerland|schweiz|suisse|"
    r"uk|united\s+kingdom|england|scotland|wales|ireland|"
    r"us|usa|united\s+states|canada|mexico|brazil|argentina|chile|colombia|"
    r"india|japan|china|singapore|australia|new\s+zealand|"
    r"benelux|nordics|baltics|iberian|iberia|saudi\s+arabia|uae|united\s+arab\s+emirates|"
    r"americas|apac|latam|anz|mena"
    r")\b",
    re.I,
)

# Brief 8 item 3 & 4 & Brief 11 Section 5: Full-text single-country / non-EU remote restrictions or UK/US/CH work permit requirements
DESC_REMOTE_RESTRICTION_PAT = re.compile(
    r"("
    r"\b(?:must\s+(?:be\s+(?:based|located)|reside|live)|candidates?\s+must\s+(?:be\s+(?:based|located)|reside|live)|only\s+open\s+to\s+residents?\s+of|eligible\s+to\s+work\s+in\s+the)\s+"
    r"(?:in\s+)?(?:the\s+)?(?:united\s+states|u\.?s\.?a?\.?\b|united\s+kingdom|u\.?k\.?\b|poland|spain|france|italy|netherlands|portugal|ireland|canada|australia|india|brazil|switzerland|austria|sweden|denmark|norway|finland|belgium)\b"
    r"|"
    r"\bremote\s*[\(\[-–:,]\s*(?:uk|u\.k\.|us|u\.s\.|usa|spain|poland|france|italy|netherlands|portugal|ireland|canada|australia|india|switzerland|austria|americas|apac|latam)(?:\s+only)?\b"
    r"|"
    r"\b(?:uk|u\.s\.|us|usa|spain|poland|france|italy|canada|australia|india|americas|apac|latam)\s+remote\s+only\b"
    r"|"
    r"\b(?:us\s+only|u\.s\.\s+only|usa\s+only|united\s+states\s+only|us\s+residents?\s+only)\b"
    r"|"
    r"\b(?:us|u\.s\.|united\s+states|uk|united\s+kingdom|swiss|switzerland)\s+(?:work\s+(?:permit|authorization|authorisation)|citizenship|right\s+to\s+work)\s+(?:is\s+)?(?:required|mandatory)\b"
    r")",
    re.I,
)

STAFFING_REPOSTERS = {
    "jobgether", "crossover", "toptal", "rocken", "rockenjobs", "bairesdev", "turing",
    "remoterocketship", "lemonio", "proxify", "gunio", "andela", "arcdev",
}

GER_FUNCTION_WORDS = set(
    "und|der|die|das|wir|sie|ihre|mit|für|bei|von|eine|auf|werden|wird|sind|als|auch|oder|dich|unser|aufgaben|erfahrung|kenntnisse".split("|")
)

ENG_FUNCTION_WORDS = set(
    "the|and|you|our|with|for|will|are|your|we|to|of|experience|team|work|role".split("|")
)

FOREIGN_LANGUAGE_FUNCTION_WORDS = {
    "French": set(
        "le|la|les|des|une|du|au|aux|et|est|dans|pour|sur|qui|que|avec|par|pas|vous|nous|notre|vos|votre|sont|cette|ces|tout|comme|mais|ou|ses|leur|leurs|être|avoir|faire|poste|entreprise|équipe|missions|profil|recherchons|rejoindre|expérience|compétences|gestion|développement|connaissance|travail|sein|cadre|offrons|chez|afin|ainsi|dont|vers|très|même|autres|tous|toutes|nos|cet|quelle|quelles|responsable|chargé|chargée|alternance|alternant|stagiaire|cdi|cdd".split("|")
    ),
    "Swedish": set(
        "och|att|det|som|på|är|av|för|med|till|har|inte|om|ett|men|från|kan|eller|så|vid|efter|också|dig|vara|vår|våra|vill|kommer|arbetar|arbete|arbetsuppgifter|tjänst|tjänsten|hos|inom|erfarenhet|kunskaper|söker|kollegor|utveckling|rollen|verksamhet|uppdrag|ansvar|kund|kunder|företag|erbjuder|ansökan|gärna|mycket|genom|mellan|utan|när|där|här|alla|andra|denna|detta|dessa".split("|")
    ),
    "Dutch": set(
        "het|een|van|op|te|dat|voor|zijn|met|niet|aan|om|ook|maar|dan|bij|uit|naar|je|jij|wij|onze|ons|jouw|uw|hebt|heeft|worden|wordt|bent|kunnen|binnen|werk|werken|functie|ervaring|kennis|zoeken|gezocht|collega|klanten|ontwikkeling|verantwoordelijk|bedrijf|samen|waar|hoe|welke|deze|dit|alle|andere|veel|goed|nieuwe|eigen|graag|bieden|solliciteer|vacature|medewerker|beheerder|adviseur".split("|")
    ),
    "Spanish": set(
        "el|la|los|las|del|se|por|un|una|para|con|su|al|lo|como|más|pero|sus|ya|este|esta|estos|estas|entre|cuando|muy|sin|sobre|también|hasta|hay|donde|quien|desde|todo|todos|nos|durante|uno|contra|otros|otras|ese|esa|eso|ante|ellos|ellas|esto|antes|algunos|algunas|unos|unas|otro|otra|tanto|mucho|muchos|poco|estar|algo|nosotros|nuestro|nuestra|nuestros|nuestras|está|están|empresa|equipo|experiencia|trabajo|puesto|buscamos|conocimientos|clientes|desarrollo|funciones|requisitos|gestión|atención|técnico|comercial".split("|")
    ),
    "Italian": set(
        "il|la|che|per|un|una|del|della|dei|delle|al|alla|ai|alle|dal|dalla|nel|nella|sul|sulla|con|su|non|si|le|gli|lo|sono|è|ha|hanno|più|anche|come|ma|se|perché|quando|dove|chi|cosa|questo|questa|questi|queste|quello|quella|quelli|quelle|nostro|nostra|nostri|nostre|vostro|vostra|suo|sua|suoi|sue|loro|noi|voi|essere|avere|fare|lavoro|azienda|esperienza|ricerca|cerchiamo|candidato|competenze|attività|gestione|clienti|sviluppo|ruolo|requisiti|inserito|risorsa|presso|settore".split("|")
    ),
    "Polish": set(
        "na|do|że|jest|się|nie|jak|ale|po|co|tak|za|od|dla|czy|przez|przy|oraz|lub|tym|tego|tej|tych|ten|ta|te|jako|będzie|są|mają|być|nasz|naszej|naszym|naszych|twój|twoja|twoje|pracy|praca|doświadczenie|znajomość|zespół|zespole|klientów|rozwoju|poszukujemy|oferujemy|wymagania|zakres|obowiązków|stanowisko|osoba|umiejętność|współpracy|firmy|firmie|wsparcie|udział|tworzenie|zarządzanie|specjalista|młodszy|starszy|inżynier|kierownik|doradca".split("|")
    ),
}

# Brief 9 Section 2: `german_required` gate patterns
MANDATORY_GER_PAT = re.compile(
    r"("
    r"\b(?:fluent|fluently|fluency|native|bilingual|proficient|proficiency|business[- ]level|business\s+fluent|professional\s+working\s+proficiency|full\s+professional\s+proficiency)\s+(?:in\s+)?(?:the\s+)?german\b"
    r"|"
    r"\bgerman\s*[\(\[:,–-]?\s*(?:c1|c2|b2|native|mother\s+tongue|fluent|fluency|business[- ]level|business\s+fluent|written\s+and\s+spoken|spoken\s+and\s+written|required|mandatory|essential)\b"
    r"|"
    r"\b(?:c1|c2|b2)\s*(?:or\s+higher\s+|/\s*(?:c1|c2)\s+|level\s+)?(?:in\s+)?german\b"
    r"|"
    r"\b(?:excellent|strong|advanced|very\s+good|good|solid|high\s+level\s+of|working)\s+(?:command|knowledge|proficiency|level|skills?)\s+(?:of|in)\s+(?:the\s+)?(?:english\s+and\s+)?german\b"
    r"|"
    r"\b(?:fluent|fluency|proficient|proficiency|native|c1|c2|b2|business[- ]level|verhandlungssicher\w*|flie[ßs]end\w*)\s+in\s+(?:both\s+)?(?:english\s+and\s+german|german\s+and\s+english)\b"
    r"|"
    r"\b(?:native|fluent)\s+german\s+speaker\b"
    r"|"
    r"\b(?:speak|speaking|write|writing|communicate\s+in)\s+(?:fluent\s+|business\s+)?german\b"
    r"|"
    r"\bgerman[- ]speaking\b"
    r"|"
    r"\bgerman\s+(?:language\s+)?(?:skills?\s+)?(?:is|are)?\s*(?:required|mandatory|essential|a\s+must|needed|necessary|prerequisite)\b"
    r"|"
    r"\b(?:required|mandatory|essential)\s+languages?\s*:\s*[^\n\.;]*\bgerman\b"
    r"|"
    r"\blanguages?\s*:\s*german\b"
    r"|"
    r"\bdeutschkenntnisse[^\n\.;]{0,40}\b(?:erforderlich|sehr\s+gut\w*|verhandlungssicher\w*|flie[ßs]end\w*|vorausgesetzt|zwingend|c1|c2|b2|muttersprach\w*)\b"
    r"|"
    r"\b(?:sehr\s+gute|gute|flie[ßs]ende|verhandlungssichere|erforderliche|zwingende|sichere|fundierte)\s+(?:deutsch-?\s+und\s+englischkenntnisse|deutschkenntnisse)\b"
    r"|"
    r"\b(?:verhandlungssicher\w*|flie[ßs]end\w*|muttersprach\w*)\s+(?:in\s+)?deutsch\w*\b"
    r"|"
    r"\bdeutsch\s+in\s+wort\s+und\s+schrift\b"
    r"|"
    r"\bfluent\b[^\n\.;]{0,35}\bgerman\b"
    r"|"
    r"\bgerman(?!\s+(?:citizenship|passport|work\s+permit|driving\s+licen[cs]e|driver'?s?\s+licen[cs]e))\b[^\n\.;]{0,35}\b(?:fluent|fluency|c1|c2|b2|native|business[- ]level|written\s+and\s+spoken|spoken\s+and\s+written|verhandlungssicher|required|mandatory|essential|must[- ]have)\b"
    r")",
    re.I,
)

PLUS_PAT = re.compile(
    r"\b(advantage|advantageous|plus|nice\s+to\s+have|nice-to-have|bonus|beneficial|preferred|ideally|desirable|von\s+vorteil|w[üu]nschenswert|optional|not\s+required|not\s+mandatory|willingness\s+to\s+learn|eager\s+to\s+learn|a1|a2|basic\s+german|elementary\s+german)\b",
    re.I,
)

SENIORITY_EXCLUDE = re.compile(
    r"\b(Director|Head\s+of|VP|Vice\s+President|Chief|Principal|Staff|Team\s+Lead|Engineering\s+Manager|Intern|Internship|Working\s+Student|Werkstudent|Praktikum|Trainee|Ausbildung|Thesis)\b",
    re.I,
)

SENIOR_TITLE_WITH_YEARS_PAT = re.compile(
    r"\b(Senior|Sr\.?|Staff|Lead|Principal)\b",
    re.I,
)

STARTUP_GENERALIST_ALLOW = re.compile(
    r"\b("
    r"Chief\s+of\s+Staff|"
    r"Founder'?s?\s+Associate|"
    r"Founding\s+(?:Customer\s+Success|Operations|Solutions|Implementation|Account\s+Manager|Product\s+Manager|Consultant)|"
    r"Business\s+Operations|"
    r"BizOps|"
    r"Generalist|"
    r"Strategy\s+(?:&|and)\s+Operations"
    r")\b",
    re.I,
)

# Brief 13 Section 1: removed broad startup titles (Chief of Staff, Founder Associate, Generalist, Business Operations, BizOps, Strategy & Operations)
STARTUP_GENERALIST_ALLOW_STRICT = re.compile(
    r"\bFounding\s+(?:Customer\s+Success|Operations|Solutions|Implementation|Account\s+Manager|Product\s+Manager|Consultant)\b",
    re.I,
)

STARTUP_KEYWORDS_PAT = re.compile(
    r"\b(seed\s+stage|series\s+[a-c]\b|pre[- ]seed|venture[- ]backed|start-?up|scale-?up|early[- ]stage)\b",
    re.I,
)

STARTUP_SEED_COMPANIES = {
    "celonis", "personio", "flixbus", "lilium", "isar aerospace", "proxima fusion",
    "alasco", "robco", "agile robots", "twaice", "helsing", "ottonova", "simscale",
    "freeletics", "riskmethods", "navvis", "workpath", "holidu", "kaia health",
    "cognigy", "kinexon", "idnow", "blinkist", "scalable capital", "trade republic",
    "forto", "enpal", "wandelbots", "deepl", "usercentrics", "yext", "databricks",
    "workato", "mulesoft", "servicenow", "salesforce", "finn", "konux", "everphone",
    "remberg", "almer technologies", "flyex", "inveox", "clerk", "zenkit", "yfood",
    "tacto", "clariness", "cozero", "presize", "droniq", "circula", "workbee",
    "kertos", "coshx", "novalnet", "deepc", "orora technologies", "quantum systems",
    "marvel fusion", "bimm", "reverion", "inquility", "smartlane", "kumavis",
    "hopr", "bluetuip", "corintis", "kewaunee", "planqc", "blacklane", "sennder",
    "chauffeur prive", "tier mobility", "blablacar", "camunda", "celigo",
    # Munich Ecosystem & VC seeds
    "xpreneurs", "unternehmertum", "tum venture labs", "werk1", "baystartup",
    "uvc partners", "hv capital", "earlybird", "picus capital", "speedinvest",
    "cherry ventures", "project a", "la famiglia", "btov", "visionaries club",
    "42cap", "acton capital", "high-tech gründerfonds", "htgf", "munich startup"
}

ENGINEER_TYPO = r"(?:engi?ee?r|enginer|enginee?r)"

ENGINEERING_ROLE_PAT = re.compile(
    rf"\b("
    rf"developer|"
    rf"software\s+{ENGINEER_TYPO}|"
    rf"backend|back-end|frontend|front-end|full[- ]?stack|"
    rf"java\b|c\+\+|golang|rust\b|devops|sre\b|site\s+reliability|"
    rf"platform\s+{ENGINEER_TYPO}|test\s+{ENGINEER_TYPO}|qa\s+{ENGINEER_TYPO}|"
    rf"computer\s+vision|machine\s+learning|ml\s+{ENGINEER_TYPO}|"
    rf"data\s+{ENGINEER_TYPO}|data\s+scientist|embedded|firmware|hardware|"
    rf"mechanical|electrical|"
    rf"process\s+{ENGINEER_TYPO}|network\s+{ENGINEER_TYPO}|"
    rf"infrastructure\s+{ENGINEER_TYPO}|cloud\s+{ENGINEER_TYPO}|"
    rf"security\s+{ENGINEER_TYPO}|robotics?\s+{ENGINEER_TYPO}|"
    rf"systems?\s+{ENGINEER_TYPO}|avionics?\s+{ENGINEER_TYPO}|"
    rf"field\s+service\s+{ENGINEER_TYPO}|application\s+{ENGINEER_TYPO}"
    rf")\b",
    re.I,
)

ENGINEERING_ROLE_ALLOW = re.compile(
    r"\b(solutions?|sales\s+engineer|pre[- ]?sales|customer|success|technical\s+account|implementation|integration|consultant|onboarding|forward\s+deployed|support|enablement|professional\s+services|delivery|mulesoft|boomi|salesforce|servicenow|hubspot|workato|ipaas|api)\b",
    re.I,
)

DOMAIN_EXCLUDE_PAT = re.compile(
    r"\b("
    r"avionics|"
    r"weapons?|"
    r"defen[cs]e|"
    r"ordnance|"
    r"aerospace\s+systems?|"
    r"powertrain|"
    r"chassis|"
    r"homologation|"
    r"sap\s+(?:mdg|fico|s/?4(?:hana)?|btp|abap|mm|sd|hcm|successfactors)|"
    r"abap|"
    r"successfactors|"
    r"workday|"
    r"oracle\s+(?:ebs|fusion)|"
    r"netsuite"
    r")\b",
    re.I,
)

DOMAIN_ALLOW_PAT = re.compile(
    r"\b(mulesoft|boomi|salesforce|servicenow|hubspot|integration\s+platform)\b",
    re.I,
)

OFF_FUNCTION_PATTERNS = [
    # Pure software engineer/developer
    re.compile(r"\b(?:software\s+(?:engineer|developer)|softwareentwickler|backend\s+(?:developer|engineer)|frontend\s+(?:developer|engineer)|full[\s-]?stack\s+(?:developer|engineer)|web\s+developer)\b", re.I),
    # Data & ML / AI engineering
    re.compile(r"\b(data\s+engineer|ml\s+engineer|machine\s+learning\s+engineer|genai\s+engineer|ai\s+engineer|data\s+scientist|scientist|researcher|research\s+scientist)\b", re.I),
    # Hardware/Embedded
    re.compile(r"\b(hardware|embedded|firmware|mechanical(?:\s+(?:engi?ee?r|enginer))?|electrical(?:\s+(?:engi?ee?r|enginer))?|process\s+(?:engi?ee?r|enginer)|fpga|pcb|chip\s+design)\b", re.I),
    # Administrative/Physical/Legal
    re.compile(r"\b(accountant|buchhalter|legal|counsel|lawyer|paralegal|jurist|general\s+counsel|payroll|lohnbuchhalter|nurse|krankenpfleger|driver|fahrer|warehouse|lagerist)\b", re.I),
    # Pure quota/sales
    re.compile(r"\b(account\s+executive|bdr|sdr|business\s+development\s+representative|sales\s+development\s+representative)\b", re.I),
    # Design / Creative / Content
    re.compile(r"\b(designer|ux\s+writer|illustrator)\b", re.I),
    # Curriculum / Instructional
    re.compile(r"\b(curriculum\s+developer|instructional\s+designer)\b", re.I),
    # Audit additions: tutor, teacher, nanny, babysitter, caregiver, cleaner, cook, chef, waiter, cashier, retail/sales assistant, delivery, courier, call center
    re.compile(r"\b(tutor|teacher|nanny|babysitter|caregiver|cleaner|cook|chef|waiter|cashier|sales\s+assistant|retail\s+assistant|delivery|courier|call\s*center(?:\s+agent)?)\b", re.I),
]

GERMAN_TITLE_WORDS = re.compile(
    r"(\b("
    r"dach|leiter(?:in)?|leitung|managerin|kaufm\w*|sachbearbeiter(?:in)?|"
    r"referent(?:in)?|spezialist(?:in)?|mitarbeiter(?:in)?|"
    r"projektleiter(?:in)?|fachkraft\w*|gesucht|anwendungsberater|bilingual|"
    r"deutschsprachig|german[- ]speaking|german[- ]speaker|"
    r"entwickler(?:in)?|systemingenieur(?:in)?|technischer|technische|techniker(?:in)?|"
    r"ingenieur(?:in)?|berater(?:in)?|beratung|vertrieb\w*|handelsvertreter\w*|zusteller\w*|"
    r"selbstst[äa]ndig\w*|verk[äa]ufer\w*|fachinformatiker\w*|"
    r"monteur|elektriker|mechaniker|angestellte|stelle|werkstudent"
    r")\b|"
    r"\w*(?:ingenieur|entwickl|techniker|kundenbetreuer|berater|handelsvertreter|fachinformatiker|sachbearbeiter|vertrieb)\w*|"
    r"english\s*/\s*german|german\s*/\s*english)",
    re.I,
)

OFF_PROFILE_TITLE_PATTERNS = re.compile(
    r"\b("
    r"assistant|assistenz|minijob|mini-job|werkstudent|"
    r"stagiair\w*|intern|internship|praktik\w*|trainee|"
    r"zusteller\w*|driver\w*|fahrer\w*|grocery\w*|cashier\w*|kassier\w*|"
    r"operator\w*|cleaner\w*|kitchen\w*|waiter\w*|babysit\w*|tutor\w*|"
    r"expert\s+model|open\s+application|initiative\s+application|"
    r"unsolicited|spontan\w*|talent\s+community|talent\s+pool|"
    r"office\s+management|model\s+trains|new\s+product\s+introduction"
    r")\b",
    re.I,
)

OFF_PROFILE_EXCEPTIONS = re.compile(
    r"\b(mulesoft|boomi|salesforce|servicenow|hubspot|integration|solutions?|customer\s+success|implementation|consultant)\b",
    re.I,
)

# Brief 9 Section 1: Role-family priors (applied unless empirical outcomes table disagrees)
ROLE_FAMILY_BOOST_PRIORS: dict[str, float] = {
    "implementation consultant": 12.0,
    "onboarding consultant": 12.0,
    "solutions consultant": 12.0,
    "solution consultant": 12.0,
    "integration consultant": 12.0,
    "technical consultant": 10.0,
    "technical account manager": 10.0,
    "customer success": 8.0,
    "technical customer success": 10.0,
    "business analyst": 8.0,
    "systems analyst": 8.0,
    "business systems analyst": 10.0,
    "pre-sales engineer": 10.0,
    "presales engineer": 10.0,
    "solutions engineer": 10.0,
    "solution engineer": 10.0,
    "implementation": 8.0,
    "integration": 8.0,
    "onboarding": 8.0,
    "delivery": 6.0,
}

ROLE_FAMILY_PENALTY_PRIORS: dict[str, float] = {
    "social media manager": -15.0,
    "social media": -12.0,
    "content manager": -15.0,
    "procurement manager": -15.0,
    "business development representative": -15.0,
    "revenue operations": -10.0,
    "inside sales": -12.0,
    "product engineer": -12.0,
    "software engineer": -12.0,
    "embedded engineering": -15.0,
    "hardware engineering": -15.0,
    "marketing": -10.0,
    "content": -10.0,
    "community": -8.0,
    "procurement": -12.0,
    "purchasing": -12.0,
    "recruiting": -12.0,
    "hr": -10.0,
    "finance": -10.0,
    "accounting": -12.0,
    "hardware": -12.0,
    "embedded": -12.0,
}

TITLE_STOPWORDS = {
    "and", "or", "the", "of", "in", "for", "to", "at", "with", "on", "a", "an",
    "mwd", "fmd", "wmd", "gn", "all", "genders", "munich", "münchen", "germany",
    "remote", "hybrid", "emea", "eu", "europe", "dach", "de", "en", "english",
    "full", "time", "fulltime", "role", "position", "job", "team",
}

# Brief 9 Section 2: `deep_stack` central stack patterns
DEEP_STACK_TITLE_PAT = re.compile(
    r"\b("
    r"kubernetes|k8s|terraform|linux\s+(?:admin(?:istrator)?|engineer|systems)|"
    r"devops\s+(?:engineer|specialist|architect)|site\s+reliability|sre\b|"
    r"cloud\s+infrastructure|aws\s+(?:devops|infrastructure|cloud\s+engineer|architect)|"
    r"sap\s+abap|abap\s+developer|abap\b|"
    r"embedded\s+(?:c|c\+\+|software|systems|linux)|"
    r"c\+\+\s+(?:developer|engineer|product\s+owner|specialist)|"
    r"kernel\s+(?:developer|engineer|hacker|programming)|"
    r"(?:ml|machine\s+learning|deep\s+learning)\s+(?:model\s+training|research|engineer)|"
    r"soc\s+(?:analyst|engineer)|penetration\s+tester|pentest(?:er|ing)?|red\s+team|"
    r"mainframe|cobol|z/os|quantum\s+integration"
    r")\b",
    re.I,
)

DEEP_STACK_DESC_PAT = re.compile(
    r"("
    r"\b(?:hands[- ]on\s+experience|deep\s+expertise|strong\s+experience|proven\s+experience|3\+\s+years|4\+\s+years|5\+\s+years|administering|managing|operating|deploying)\s+[^\n\.;]{0,55}\b"
    r"(?:kubernetes\s+clusters?|terraform\s+modules?|linux\s+(?:kernel|system\s+administration|servers?)|sap\s+abap|embedded\s+c(?:/c\+\+|\+\+)?|kernel\s+development|training\s+(?:deep\s+learning|ml|llm)\s+models?|penetration\s+testing|soc\s+monitoring|mainframe|z/os|cobol)\b"
    r"|"
    r"\b(?:kubernetes|terraform)\b[^\n\.;]{0,50}\b(?:terraform|kubernetes|helm|eks|aks|gke|ansible|linux\s+administration)\b"
    r"|"
    r"\b(?:sap\s+abap|embedded\s+c/c\+\+|linux\s+kernel\s+development|mainframe\s+z/os)\b"
    r")",
    re.I,
)

# Brief 9 Section 2: `clearance` gate patterns
CLEARANCE_PAT = re.compile(
    r"("
    r"\b(?:süg|sueg)\b(?:\s*(?:1|2|3|i|ii|iii|level|stufe|clearance|überprüfung))?"
    r"|"
    r"\bsicherheits[üu]berpr[üu]fung(?:\s+nach\s+s[üu]g)?\b"
    r"|"
    r"\b(?:sc|dv|nato\s+secret|eu\s+secret|cosmic\s+top\s+secret)\s+clearance\b"
    r"|"
    r"\bsecurity\s+clearance\b[^\n\.;]{0,40}\b(?:required|mandatory|eligible|must|needed|süg|sueg|nato|secret)\b"
    r"|"
    r"\b(?:eligible\s+for|obtain|hold|require[sd]?|undergo)\s+(?:a\s+)?(?:german\s+|national\s+|nato\s+|military\s+)?security\s+clearance\b"
    r"|"
    r"\bgerman\s+citizenship\s+(?:is\s+)?(?:required|mandatory|essential|needed)\b"
    r"|"
    r"\b(?:deutsche\s+staatsb[üu]rgerschaft|deutsche\s+staatsangeh[öo]rigkeit)\b"
    r")",
    re.I,
)


def normalise_for_matching(text: str) -> str:
    """Lowercase and strip non-alphanumerics."""
    if not text:
        return ""
    # Strip common gender markers
    t = re.sub(r"\b(m/w/d|f/m/d|d/m/w|m/f/d|all\s+genders|gn|d|w|m)\b", "", text, flags=re.I)
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", t)
    return re.sub(r"[^a-z0-9]", "", t.lower())


def tokenize_title_terms(title: str) -> Tuple[list[str], list[str]]:
    """Extract normalized unigrams and bigrams from a job title."""
    if not title:
        return [], []
    cleaned = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", title.lower())
    cleaned = re.sub(r"\b(m/w/d|f/m/d|w/m/d|m/f/x|all\s+genders|gn)\b", " ", cleaned, flags=re.I)
    words = [w for w in re.findall(r"[a-z0-9\+#]+", cleaned) if len(w) >= 2 and w not in TITLE_STOPWORDS]
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
    return words, bigrams


def write_filters_json(target_path: Optional[Path] = None) -> Path:
    """
    Exports filters to data/filters.json for sharing with the Node/Claude pipeline.
    """
    out_path = target_path or (DATA_DIR / "filters.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "german_title_words": [
            "handelsvertreter", "zusteller", "berater", "beraterin", "mitarbeiter",
            "mitarbeiterin", "sachbearbeiter", "sachbearbeiterin", "kaufmann", "kauffrau",
            "leiter", "leiterin", "referent", "referentin", "entwickler", "entwicklerin",
            "vertrieb", "projektleiter", "projektleiterin", "spezialist", "spezialistin",
            "fachinformatiker", "ingenieur", "ingenieurin", "techniker", "technikerin",
            "fachkraft", "selbstständig", "verkäufer", "verkäuferin"
        ],
        "off_profile_title_words": [
            "assistant", "assistenz", "minijob", "mini-job", "werkstudent",
            "stagiair", "intern", "internship", "praktik", "trainee",
            "zusteller", "driver", "fahrer", "grocery", "cashier", "kassier",
            "operator", "expert model", "open application", "initiative application",
            "unsolicited", "spontan", "talent community", "talent pool",
            "office management", "cleaner", "kitchen", "waiter", "babysit", "tutor",
            "model trains", "new product introduction"
        ],
        "mandatory_german_patterns": [
            "fluent", "fluently", "fluency", "native", "bilingual", "proficient",
            "proficiency", "c1", "c2", "b2", "speak german", "german speaking",
            "german-speaking", "verhandlungssicher", "fließend", "sehr gute deutsch"
        ],
        "soft_words": [
            "plus", "advantage", "nice to have", "bonus", "beneficial",
            "preferred", "ideally", "desirable", "von vorteil", "wünschenswert", "optional"
        ]
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return out_path



# 5 Nov 2026: German cities other than Munich, for "<city> (Remote)" locations (Germany-remote roles)
GERMAN_CITY_RE = re.compile(r"\b(berlin|hamburg|frankfurt|k[oö]ln|cologne|d[uü]sseldorf|stuttgart|leipzig|dresden|hannover|hanover|n[uü]rnberg|nuremberg|bonn|essen|dortmund|karlsruhe|mannheim|heidelberg|bremen|augsburg|regensburg|ingolstadt|freiburg|aachen|m[uü]nster|potsdam|wiesbaden|mainz|germany|deutschland)\b", re.I)

class ProfileMatcher:
    def __init__(self, profile_config: ProfileConfig):
        self.config = profile_config
        self.applied_history: set[Tuple[str, str]] = set()
        self.submitted_prefix14_history: set[Tuple[str, str]] = set()
        self.submitted_company_counts: dict[str, int] = defaultdict(int)
        self.dedupe_skipped_counts: dict[str, int] = {"exact_dup": 0, "prefix14_dup": 0, "company_cap_3": 0}
        self.ger_skip_list: set[str] = set()
        # Brief 12 section 7: pipeline sets 300; unit tests with tiny fixtures keep 0 (disabled)
        self.min_description_chars: int = 0
        self.empirical_term_weights: dict[str, float] = {}
        self.combined_term_weights: dict[str, float] = {}
        self.term_stats: dict[str, dict[str, int]] = {}
        self.role_rules: Optional[dict] = None
        self.role_rules_family: Optional[re.Pattern] = None
        self.role_rules_family_not: Optional[re.Pattern] = None
        self.role_rules_seniority: Optional[re.Pattern] = None
        self.role_rules_seniority_ok: Optional[re.Pattern] = None
        self.role_rules_years_block: Optional[re.Pattern] = None
        self.role_rules_german_title: Optional[re.Pattern] = None
        self.role_rules_munich: Optional[re.Pattern] = None
        self.role_rules_remote_ok: Optional[re.Pattern] = None
        self._default_family_re: Optional[re.Pattern] = None
        self._default_family_not_re: Optional[re.Pattern] = None
        self._default_seniority_re: Optional[re.Pattern] = None
        self._default_seniority_ok_re: Optional[re.Pattern] = None
        self.max_vague_fetches_per_cycle: int = 150
        self.vague_fetches_this_cycle: int = 0
        self.vague_description_cache: dict[str, str] = {}
        self._vague_lock = threading.Lock()
        self._init_location_rules_from_file()
        self._negative_keyword_patterns: list[Tuple[str, re.Pattern]] = [
            (kw, re.compile(rf"\b{re.escape(kw)}\b", re.I))
            for kw in self.config.negative_keywords
        ]

        self.reload_data()
        try:
            write_filters_json()
        except Exception:
            pass

        # Compile ATS keyword categories
        self._categories = {
            "Integration & iPaaS": {
                kw: (weight, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
                for kw, weight in self.config.integration_keywords.items()
            },
            "Enterprise Platforms": {
                kw: (weight, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
                for kw, weight in self.config.platform_keywords.items()
            },
            "AI & Security": {
                kw: (weight, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
                for kw, weight in self.config.ai_security_keywords.items()
            },
            "Consulting & Functional": {
                kw: (weight, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
                for kw, weight in self.config.consulting_keywords.items()
            },
            "DevOps & Delivery": {
                kw: (weight, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
                for kw, weight in self.config.tooling_keywords.items()
            },
        }

        self._role_patterns = [
            (role, re.compile(rf"\b{re.escape(role)}\b", re.IGNORECASE))
            for role in self.config.target_roles
        ]
        self._startup_seeds = {normalise_for_matching(s) for s in STARTUP_SEED_COMPANIES}

    def tag_startup(self, job: JobPosting) -> None:
        if job.source in ("wellfound", "vc", "join") or job.ats in ("join",):
            job.is_startup = 1
            if not job.startup_source:
                job.startup_source = job.source
            return

        if job.startup_source:
            job.is_startup = 1
            return

        c_norm = normalise_for_matching(job.company)
        c_disp = normalise_for_matching(job.company_display or "")
        for s in self._startup_seeds:
            if s and (s in c_norm or s in c_disp or c_norm in s):
                job.is_startup = 1
                job.startup_source = "seed_list"
                return

        combined = f"{job.title} {job.description}"
        if STARTUP_KEYWORDS_PAT.search(combined):
            job.is_startup = 1
            job.startup_source = "description"
            return

    def _load_applied_history(self) -> set[Tuple[str, str]]:
        history = set()
        self.submitted_prefix14_history = set()
        self.submitted_company_counts = defaultdict(int)
        path = get_applied_history_path()
        if not path.exists():
            return history
        with open(path, mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                c = normalise_for_matching(row.get("company", ""))
                r = normalise_for_matching(row.get("role", ""))
                st = (row.get("status") or "").strip()
                if c and r:
                    history.add((c, r))
                    if st.lower().startswith("submitted"):
                        self.submitted_prefix14_history.add((c, r[:14]))
                        self.submitted_company_counts[c] += 1
        return history

    def _load_outcomes_and_weights(self) -> None:
        """
        Brief 9 Section 1:
        Read outcomes.csv and applied_history.csv (read-only) and build a title-keyword weight table
        combined with the role-family prior.
        """
        stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"sent": 0, "rejected": 0, "replied": 0, "interview": 0, "offer": 0}
        )

        # 1. Read applied_history.csv for sent counts (status starting with 'Submitted' or 'Applied')
        ah_path = get_applied_history_path()
        if ah_path.exists():
            try:
                with open(ah_path, mode="r", encoding="utf-8", errors="ignore") as f:
                    for row in csv.DictReader(f):
                        role = row.get("role") or ""
                        status = (row.get("status") or "").strip().lower()
                        if not (status.startswith("submitted") or status.startswith("applied")):
                            continue
                        words, bigrams = tokenize_title_terms(role)
                        terms = set(words + bigrams)
                        for term in terms:
                            stats[term]["sent"] += 1
            except Exception:
                pass

        # 2. Read outcomes.csv for real post-application outcomes (Replied, Interview, Rejected, Offer, Withdrawn)
        out_path = get_outcomes_path()
        if out_path.exists():
            try:
                with open(out_path, mode="r", encoding="utf-8", errors="ignore") as f:
                    for row in csv.DictReader(f):
                        title = row.get("title") or ""
                        status = (row.get("status") or "").strip().lower()
                        words, bigrams = tokenize_title_terms(title)
                        terms = set(words + bigrams)
                        for term in terms:
                            stats[term]["sent"] = max(stats[term]["sent"], 1)
                            if status == "rejected":
                                stats[term]["rejected"] += 1
                            elif status == "replied":
                                stats[term]["replied"] += 1
                            elif status == "interview":
                                stats[term]["interview"] += 1
                            elif status == "offer":
                                stats[term]["offer"] += 1
            except Exception:
                pass

        self.term_stats = dict(stats)
        empirical: dict[str, float] = {}
        for term, s in stats.items():
            pos = s["interview"] + s["offer"]
            rej = s["rejected"]
            sent = s["sent"]
            if pos >= 1:
                empirical[term] = 10.0
            elif sent >= 3 and pos == 0 and rej >= 3:
                empirical[term] = -10.0
            elif rej >= 1 and pos == 0 and term in ROLE_FAMILY_PENALTY_PRIORS:
                empirical[term] = min(-10.0, ROLE_FAMILY_PENALTY_PRIORS[term])

        self.empirical_term_weights = empirical

        # Combine role-family priors with empirical weights (role-family prior applied unless empirical table disagrees)
        combined: dict[str, float] = {}
        for term, w in ROLE_FAMILY_PENALTY_PRIORS.items():
            combined[term] = w
        for term, w in ROLE_FAMILY_BOOST_PRIORS.items():
            combined[term] = w
        for term, emp_w in empirical.items():
            combined[term] = emp_w

        self.combined_term_weights = combined

        # Brief 13 Section 1: load_role_rules() is called at the start of each cycle
        pass

        # Refresh negative keyword patterns
        self._negative_keyword_patterns = [
            (kw, re.compile(rf"\b{re.escape(kw)}\b", re.I))
            for kw in self.config.negative_keywords
        ]

    def compute_title_weight(self, title: str) -> float:
        """
        Compute the net title_weight adjustment from Section 1 (empirical outcome weights + role-family priors).
        Empirical word penalties cap at -25.0.
        """
        if not title:
            return 0.0
        t_low = title.lower()
        words, bigrams = tokenize_title_terms(title)

        # First check if a role-family boost prior matches the title (unless empirical table disagrees on that phrase)
        matched_boost_phrase = None
        prior_boost = 0.0
        for phrase, boost_val in sorted(ROLE_FAMILY_BOOST_PRIORS.items(), key=lambda x: -len(x[0])):
            if re.search(rf"\b{re.escape(phrase)}\b", t_low, re.I):
                if self.empirical_term_weights.get(phrase, 0.0) < 0:
                    continue
                matched_boost_phrase = phrase
                prior_boost = max(prior_boost, boost_val)
                break

        empirical_penalty = 0.0
        empirical_boost = 0.0
        used_terms: set[str] = set()
        boost_phrase_words = set(matched_boost_phrase.split()) if matched_boost_phrase else set()

        # Check empirical bigrams then unigrams
        for bg in bigrams:
            if bg in self.empirical_term_weights:
                w = self.empirical_term_weights[bg]
                used_terms.add(bg)
                if w < 0:
                    if matched_boost_phrase and bg == matched_boost_phrase:
                        empirical_penalty += w
                    elif not matched_boost_phrase:
                        empirical_penalty += w
                else:
                    empirical_boost = max(empirical_boost, w)

        for wd in set(words):
            if wd in self.empirical_term_weights:
                if any(wd in bg.split() for bg in used_terms):
                    continue
                w = self.empirical_term_weights[wd]
                used_terms.add(wd)
                if w < 0:
                    # Do not let a generic sub-word like "consultant" or "engineer" negate a boosted role family like "Implementation Consultant"
                    if wd in boost_phrase_words:
                        continue
                    empirical_penalty += w
                else:
                    empirical_boost = max(empirical_boost, w)

        empirical_penalty = max(-25.0, empirical_penalty)

        prior_penalty = 0.0
        for phrase, pen_val in sorted(ROLE_FAMILY_PENALTY_PRIORS.items(), key=lambda x: -len(x[0])):
            if re.search(rf"\b{re.escape(phrase)}\b", t_low, re.I):
                if self.empirical_term_weights.get(phrase, 0.0) > 0:
                    continue
                prior_penalty = min(prior_penalty, pen_val)
                break

        total_neg = max(-25.0, empirical_penalty + prior_penalty)
        total_pos = max(empirical_boost, prior_boost)
        return round(total_pos + total_neg, 1)

    def get_top_title_weights(self, n: int = 15) -> Tuple[list[Tuple[str, float, dict]], list[Tuple[str, float, dict]]]:
        """Return the top N most penalised and top N most boosted terms for the run report."""
        penalised = []
        boosted = []
        for term, weight in self.combined_term_weights.items():
            st = self.term_stats.get(term, {"sent": 0, "rejected": 0, "replied": 0, "interview": 0, "offer": 0})
            if weight < 0:
                penalised.append((term, weight, st))
            elif weight > 0:
                boosted.append((term, weight, st))

        penalised.sort(key=lambda x: (x[1], -x[2].get("rejected", 0), -x[2].get("sent", 0), x[0]))
        boosted.sort(key=lambda x: (-x[1], -x[2].get("interview", 0), -x[2].get("replied", 0), x[0]))
        return penalised[:n], boosted[:n]

    def _load_ger_skip_list(self) -> set[str]:
        skip = set(STAFFING_REPOSTERS)
        exempt_tech = {"amazon", "aws", "google", "microsoft", "apple", "meta", "salesforce"}
        path = get_ger_skip_list_path()
        if not path.exists():
            return skip
        with open(path, mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("scope", "").strip().lower() == "all":
                    c = normalise_for_matching(row.get("company", ""))
                    if c and c not in exempt_tech:
                        skip.add(c)
        return skip

    def reload_data(self) -> None:
        """Reload applied history, outcomes weights, and skip list live from disk."""
        self.dedupe_skipped_counts = {"exact_dup": 0, "prefix14_dup": 0, "company_cap_3": 0}
        self.applied_history = self._load_applied_history()
        self.ger_skip_list = self._load_ger_skip_list()
        self._load_outcomes_and_weights()

    # --------------------------------------------------------------------------
    # GATE EVALUATIONS
    # --------------------------------------------------------------------------

    def check_gate_duplicate(self, company: str, title: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 5:
        1. Exact match in applied_history.csv
        2. Normalised company + first 14 chars of normalised title match a 'Submitted' row
        3. Company cap of 3 sent ('Submitted') applications in total (Amazon exempt)
        """
        c_norm = normalise_for_matching(company)
        t_norm = normalise_for_matching(title)
        if not c_norm:
            return True, None

        if (c_norm, t_norm) in self.applied_history:
            self.dedupe_skipped_counts["exact_dup"] += 1
            return False, f"In applied_history.csv ({company} - {title})"

        t_prefix14 = t_norm[:14]
        if t_prefix14 and (c_norm, t_prefix14) in self.submitted_prefix14_history:
            self.dedupe_skipped_counts["prefix14_dup"] += 1
            return False, f"Submitted prefix-14 match in applied_history.csv ({company} - '{t_prefix14}')"

        is_amazon = "amazon" in c_norm or "aws" == c_norm
        if not is_amazon and self.submitted_company_counts.get(c_norm, 0) >= 3:
            self.dedupe_skipped_counts["company_cap_3"] += 1
            return False, f"Company reached 3-application Submitted cap ({company}: {self.submitted_company_counts[c_norm]} sent)"

        return True, None

    def check_gate_location(
        self,
        loc_str: Optional[str],
        title_str: str = "",
        desc_str: str = "",
        url_str: str = "",
        location_source: Optional[str] = None,
        job: Optional[JobPosting] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Brief 8 Location Gate:
        Sets `job.location_scope` to:
          - 'munich' for Munich & 23 suburbs
          - 'remote-de' for Germany/DACH remote
          - 'remote-eu' for fully remote EU/EMEA/CET/Europe roles
        """
        raw_loc = (loc_str or "").strip()
        is_unknown_loc = (not raw_loc) or (location_source == "unknown")
        loc = raw_loc.lower()
        tit = (title_str or "").lower().strip()
        desc_clean = re.sub(r"<[^>]+>", " ", html.unescape(desc_str or ""))
        desc_low = desc_clean.lower()
        combined = f"{loc} {tit}"
        is_non_german_domain = bool(NON_GERMAN_COUNTRY_TLD_RE.search(url_str or ""))

        def _set_scope(scope: Optional[str]) -> None:
            if job is not None:
                job.location_scope = scope

        # 6 Nov 2026: a location that names only a non-European region ("Anywhere in Latin America (Remote)", "Remote - APAC")
        # is not open to him, even when it says remote.
        if (re.search(r"\b(latin america|latam|north america|south america|americas|apac|asia|asia[- ]pacific|africa|middle east|mena|oceania|australia|new zealand)\b", loc)
                and not re.search(r"\b(europe|eu|emea|germany|deutschland|dach|munich|münchen|worldwide|global)\b", loc)):
            _set_scope(None)
            return False, f"Location rejected: region outside Europe ({loc_str})"

        has_munich_in_loc_or_tit = any(re.search(rf"\b{re.escape(sub)}\b", combined) for sub in MUNICH_SUBURBS) or bool(MUNICH_POSTAL_CODES_RE.search(combined))
        has_other_city_in_loc_or_tit = any(re.search(rf"\b{re.escape(c)}\b", combined) for c in OTHER_CITIES)

        # 5 Nov 2026: a German city marked remote in the location field ("Berlin (Remote)", "Remote - Hamburg") is a
        # Germany-remote role, which Mattia can do from Munich; hybrid / on-site in another city stays rejected.
        if (has_other_city_in_loc_or_tit and not has_munich_in_loc_or_tit
                and GERMAN_CITY_RE.search(loc) and re.search(r"\b(remote|home[- ]?office|fully\s+remote)\b", loc, re.I)
                and not re.search(r"\b(hybrid|on[- ]?site|office[- ]based)\b", loc, re.I)
                and not re.search(r"\b(hybrid|on[- ]?site)\b", tit, re.I)):
            _set_scope("remote-de")
            return True, None

        # 6 Nov 2026: multi-location postings that list a Europe-/Germany-wide remote option next to foreign cities
        # ("Remote (Europe); Remote (Spain); Lisbon") are open to him; take that segment instead of rejecting on the city.
        _territory_title = bool(re.search(r"(?:^|[\s\(\[-])(?:uk\s*(?:&|/)\s*i(?:reland)?|uki|uk|us|usa|apac|americas|latam|anz|france|spain|netherlands|benelux|nordics|italy|poland|portugal|belgium|switzerland|austria)(?:[\s\)\]-]|$)", tit, re.I))
        for seg in ([] if _territory_title else re.split(r"\s*(?:;|\||\s/\s)\s*", loc)):
            if re.fullmatch(r"\s*(?:remote|fully remote)\s*[\(\-,:]?\s*(?:germany|deutschland|dach)\)?\s*|\s*(?:germany|deutschland|dach)\s*[\(\-,]?\s*remote\)?\s*", seg):
                _set_scope("remote-de")
                return True, None
            if re.fullmatch(r"\s*(?:remote|fully remote)\s*[\(\-,:]?\s*(?:europe|eu|emea|european union|cet)\)?\s*|\s*(?:europe|eu|emea|european union)\s*[\(\-,]?\s*remote\)?\s*", seg):
                _set_scope("remote-eu")
                return True, None

        # Brief 14 Section A2: At Gate 2, a location passes when `munich` matches it or `remote_ok` matches it,
        # in addition to the current logic. Fixes "Remote (Germany eligible)", "EU/UK - Remote", "Remote; Europe; ...", "Berlin, Germany (Hybrid) (Remote)".
        if raw_loc and not _territory_title:
            if self.role_rules_munich and self.role_rules_munich.search(raw_loc):
                _set_scope("munich")
                return True, None
            _single_country_eu_tag = bool(
                re.search(
                    r"\bremote\s*[-–:]\s*(?:spain|france|italy|poland|netherlands|portugal|belgium|sweden|norway|denmark|finland|ireland)\s*\(\s*eu\s*\)",
                    loc,
                    re.I,
                )
            )
            if _single_country_eu_tag and not has_munich_in_loc_or_tit:
                _set_scope(None)
                return False, f"Location rejected: remote limited to single foreign country ({loc_str or title_str})"
            if self.role_rules_remote_ok and self.role_rules_remote_ok.search(raw_loc) and not is_non_german_domain:
                m_restr = DESC_REMOTE_RESTRICTION_PAT.search(f"{raw_loc} {title_str} {desc_clean}")
                if m_restr:
                    quote = m_restr.group(0).strip()
                    if not re.search(r"\b(germany|deutschland|eu\b|europe|emea)\b", quote, re.I):
                        _set_scope(None)
                        return False, f"Location rejected: restricted to foreign country/permit ('{quote[:100]}')"
                if re.search(r"\b(germany|deutschland|dach)\b", raw_loc, re.I):
                    _set_scope("remote-de")
                else:
                    _set_scope("remote-eu")
                return True, None

        # Non-Munich city without Munich is always rejected (including "Hybrid - Berlin", "London", etc.)
        if has_other_city_in_loc_or_tit and not has_munich_in_loc_or_tit:
            _set_scope(None)
            return False, f"Location rejected: contains non-Munich city without Munich ({loc_str or title_str})"

        # Check if title explicitly restricts to a foreign territory (e.g. "Remote - UK", "- US", "ANZ", "APAC", "UK&I", "BeNeLux")
        if re.search(
            r"(?:^|[\s\(\[-])(?:remote\s*[-–]\s*|[-–]\s*|\()?(?:uk\s*(?:&|/)\s*i(?:reland)?|uki|uk|u\.k\.|us|u\.s\.|usa|apac|amer|americas|latam|anz|australia|canada|mexico|india|france|spain|netherlands|benelux|nordics|baltics|italy|poland|sweden|norway|denmark|finland|belgium|portugal|switzerland|austria)(?:\s+only)?(?:[\s\)\]-]|$)",
            tit,
            re.I,
        ) and not has_munich_in_loc_or_tit and not re.search(r"\b(germany|deutschland|dach|emea|europe|eu)\b", tit, re.I):
            _set_scope(None)
            return False, f"Location rejected: title explicitly targets foreign territory ({title_str})"

        # Check if location or title restricts to a single non-German country (Brief 11 Section 5)
        # e.g. "Remote - Spain", "Remote UK only", "Remote - France", "Remote - Italy", "Remote - US"
        if not has_munich_in_loc_or_tit and not re.search(r"\b(germany|deutschland|munich|münchen|europe|emea|\beu\b)\b", combined, re.I):
            if re.search(
                r"\b(?:remote\s*[-–—/:(]\s*|[-–—/:(]\s*remote\b|\b)(?:uk\s*(?:&|/)\s*i(?:reland)?|uki|uk|u\.k\.|us|u\.s\.|usa|spain|france|italy|poland|netherlands|sweden|norway|denmark|finland|belgium|portugal|switzerland|austria|ireland|canada|australia|india|brazil|mexico|singapore|japan)(?:\s*(?:only|resident|based|\)))?\b",
                combined,
                re.I,
            ):
                _set_scope(None)
                return False, f"Location rejected: remote limited to single foreign country ({loc_str or title_str})"

        # Brief 8 item 3 & 4: Check full posting text for single-country / US / UK / non-EU remote restrictions
        if not has_munich_in_loc_or_tit:
            m_restr = DESC_REMOTE_RESTRICTION_PAT.search(f"{raw_loc} {title_str} {desc_clean}")
            if m_restr:
                quote = m_restr.group(0).strip()
                if not re.search(r"\b(germany|deutschland|eu\b|europe|emea)\b", quote, re.I):
                    _set_scope(None)
                    return False, f"Location rejected: restricted to foreign country/permit ('{quote[:100]}')"

        # (a) Location field or title names Munich or a Munich suburb -> 'munich'
        if has_munich_in_loc_or_tit:
            if not is_non_german_domain or bool(re.search(r"\b(germany|deutschland|munich|münchen)\b", combined, re.I)):
                _set_scope("munich")
                return True, None

        # Foreign country in location without Munich/Germany/EU/EMEA/Europe
        if (
            FOREIGN_COUNTRIES_PAT.search(loc)
            and not has_munich_in_loc_or_tit
            and not re.search(r"\b(germany|deutschland|dach|europe|emea|eu|cet)\b", loc, re.I)
        ):
            _set_scope(None)
            return False, f"Location rejected: foreign country in location ({loc_str})"

        # Section 3 (Brief 7b): Career sites on a non-German domain (.fr, .se, .nl, .es, .it, .pl, .dk, etc.)
        # need an explicit Munich/Germany location in the posting to pass.
        if is_non_german_domain:
            has_explicit_de = has_munich_in_loc_or_tit or bool(re.search(r"\b(germany|deutschland)\b", loc, re.I))
            if not has_explicit_de:
                _set_scope(None)
                return False, f"Location rejected: non-German domain requires explicit Munich/Germany location ({url_str})"

        # Reject hybrid or on-site roles outside Munich even if they say Germany/Europe
        if re.search(r"\b(on[- ]?site\s+only|100%\s+on[- ]?site)\b", combined, re.I):
            _set_scope(None)
            return False, f"Location rejected: on-site outside Munich ({loc_str})"

        # (b) Explicit EU / EMEA / CET remote or timezone indicators (Brief 11 Section 5)
        is_eu_remote_loc = bool(
            re.search(
                r"\b(?:remote\s*[\(\[\-–—/,:]\s*(?:europe|eu|emea)|(?:europe|emea)\s*[\(\[\-–—/,:]\s*remote|remote\s+(?:within|across|in)\s+(?:europe|the\s+eu|emea)|remote,\s*any\s+eu\s+country|eu\s*timezone|european\s*timezone|cet\s*/\s*cest|cet\b|cest\b)\b",
                loc,
                re.I,
            )
        )
        if is_eu_remote_loc:
            m_restr = DESC_REMOTE_RESTRICTION_PAT.search(f"{raw_loc} {title_str} {desc_clean}")
            if m_restr:
                quote = m_restr.group(0).strip()
                if not re.search(r"\b(germany|deutschland|eu\b|europe|emea)\b", quote, re.I):
                    _set_scope(None)
                    return False, f"Location rejected: restricted to foreign country/permit ('{quote[:100]}')"
            _set_scope("remote-eu")
            return True, None

        # (c) Worldwide / Anywhere (Brief 11 Section 5)
        is_worldwide_or_anywhere = bool(
            re.search(r"\b(worldwide|anywhere|global\s+remote|remote\s*[-–—/]?\s*worldwide|remote\s*[-–—/]?\s*anywhere)\b", loc, re.I)
        )
        if is_worldwide_or_anywhere:
            m_restr = DESC_REMOTE_RESTRICTION_PAT.search(f"{raw_loc} {title_str} {desc_clean}")
            if m_restr:
                quote = m_restr.group(0).strip()
                if not re.search(r"\b(germany|deutschland|eu\b|europe|emea)\b", quote, re.I):
                    _set_scope(None)
                    return False, f"Location rejected: restricted to foreign country/permit ('{quote[:100]}')"
            if re.search(r"\b(germany|deutschland|dach)\b", desc_low, re.I):
                _set_scope("remote-de")
            else:
                _set_scope("remote-eu")
            return True, None

        loc_or_tit_has_remote = bool(
            re.search(r"\b(remote|home[- ]office|telecommut\w+|work\s+from\s+home|anywhere|distributed)\b", combined, re.I)
        )
        loc_has_de = bool(re.search(r"\b(germany|deutschland|dach|\bde\b)\b", combined, re.I))
        loc_has_eu = bool(
            re.search(
                r"\b(emea|europe|european\s+union|\beu\b|any\s+eu\s+country|cet\b|central\s+european\s+time|germany\s+eligible)\b",
                combined,
                re.I,
            )
        )
        loc_restricted_foreign = bool(FOREIGN_COUNTRIES_PAT.search(loc)) and not (loc_has_de or loc_has_eu)

        # (d) Germany / DACH remote -> 'remote-de'
        if loc_or_tit_has_remote and loc_has_de and not loc_restricted_foreign:
            _set_scope("remote-de")
            return True, None

        # (e) EU / EMEA / CET remote -> 'remote-eu'
        if loc_or_tit_has_remote and loc_has_eu and not loc_restricted_foreign:
            _set_scope("remote-eu")
            return True, None

        # Brief 14 Section A2: Vague locations get a second look via description
        if is_vague_location(raw_loc):
            if desc_low.strip():
                text_all = f"{tit} {desc_low}"
                # 1. Munich: description names a Munich-area place (munich pattern or MUNICH_SUBURBS)
                has_munich_desc = (
                    bool(self.role_rules_munich and self.role_rules_munich.search(text_all))
                    or any(re.search(rf"\b{re.escape(sub)}\b", text_all) for sub in MUNICH_SUBURBS)
                    or bool(MUNICH_POSTAL_CODES_RE.search(text_all))
                )
                if has_munich_desc:
                    _set_scope("munich")
                    if job is not None:
                        job.location_source = "description"
                    return True, None

                # 2. Remote EU: description says remote work is open to Germany, EU or EMEA (remote_ok) and not restricted
                m_restr = DESC_REMOTE_RESTRICTION_PAT.search(f"{raw_loc} {title_str} {desc_clean}")
                restricted = bool(m_restr) and not re.search(r"\b(germany|deutschland|eu\b|europe|emea)\b", m_restr.group(0), re.I)
                if not restricted and not is_non_german_domain:
                    has_remote_ok_desc = (
                        bool(self.role_rules_remote_ok and self.role_rules_remote_ok.search(text_all))
                        or bool(
                            re.search(
                                r"\b(?:remote|home[- ]?office|work\s+from\s+home|mobile[s]?\s+arbeiten)\b[^.]{0,80}\b(?:germany|deutschland|dach|europe|eu|emea|cet)\b|"
                                r"\b(?:germany|deutschland|dach|europe|eu|emea)\b[^.]{0,60}\b(?:remote|home[- ]?office)\b",
                                text_all,
                                re.I,
                            )
                        )
                    )
                    if has_remote_ok_desc:
                        _set_scope("remote-de" if re.search(r"\b(germany|deutschland|dach)\b", f"{loc} {text_all}", re.I) else "remote-eu")
                        if job is not None:
                            job.location_source = "description"
                        return True, None

                # 3. Germany-wide: location is germany, deutschland, deutschlandweit or bundesweit and description names no other city
                loc_trimmed = loc.strip(" .,;")
                if loc_trimmed in {"germany", "deutschland", "deutschlandweit", "bundesweit", "germany, de", "deutschland, de"}:
                    desc_has_other_city = (
                        any(re.search(rf"\b{re.escape(c)}\b", desc_low) for c in OTHER_CITIES)
                        or bool(GERMAN_CITY_RE.search(desc_low))
                    )
                    if not desc_has_other_city and not restricted:
                        _set_scope("germany-wide")
                        if job is not None:
                            job.location_source = "description"
                        return True, None

                # 4. Otherwise: reject with code location and exact A2 reason
                _set_scope(None)
                return False, "vague location, no Munich or remote-EU signal in description"

            # Description is empty: preserve 'Germany' alone message for direct empty-description calls
            is_germany_alone = bool(re.fullmatch(r"(?:germany|deutschland|de|germany,\s*de|deutschland,\s*de)", loc, re.I))
            if is_germany_alone and not loc_or_tit_has_remote:
                _set_scope(None)
                return False, f"Location rejected: 'Germany' alone without Munich or remote ({loc_str})"

            _set_scope(None)
            return False, "vague location, no Munich or remote-EU signal in description"

        is_germany_alone = bool(re.fullmatch(r"(?:germany|deutschland|de|germany,\s*de|deutschland,\s*de)", loc, re.I))
        if is_germany_alone and not loc_or_tit_has_remote:
            _set_scope(None)
            return False, f"Location rejected: 'Germany' alone without Munich or remote ({loc_str})"

        _set_scope(None)
        return False, f"Location rejected: not in Munich or eligible Germany/EU remote ({loc_str})"

    def _init_location_rules_from_file(self) -> None:
        """Load munich/remote_ok and default role regexes from role_rules.json if present without enabling strict Gate 2b unless load_role_rules() is called."""
        rules = load_role_rules()
        if not rules:
            return
        try:
            if rules.get("munich"):
                self.role_rules_munich = re.compile(rules["munich"], re.I)
            if rules.get("remote_ok"):
                self.role_rules_remote_ok = re.compile(rules["remote_ok"], re.I)
            self._default_family_re = re.compile(rules["family"], re.I)
            self._default_family_not_re = re.compile(rules["family_not"], re.I)
            self._default_seniority_re = re.compile(rules["seniority"], re.I)
            self._default_seniority_ok_re = re.compile(rules["seniority_ok"], re.I)
        except Exception:
            pass

    def reset_vague_fetch_budget(self, max_fetches: int = 150) -> None:
        with self._vague_lock:
            self.max_vague_fetches_per_cycle = max_fetches
            self.vague_fetches_this_cycle = 0

    def try_claim_vague_fetch(self, job_id: str) -> bool:
        """Claim 1 of the 150 vague-location description fetches per cycle; each job_id is fetched at most once."""
        if not job_id:
            return False
        with self._vague_lock:
            if job_id in self.vague_description_cache:
                return False
            if self.vague_fetches_this_cycle >= self.max_vague_fetches_per_cycle:
                return False
            self.vague_fetches_this_cycle += 1
            self.vague_description_cache[job_id] = ""
            return True

    def should_fetch_for_vague_location(self, loc_str: Optional[str], title: Optional[str]) -> bool:
        """
        Brief 14 Section A2:
        When to fetch the description: the location is vague, the title passes role-rules and
        negative keywords, and the title names no other region.
        """
        if not is_vague_location(loc_str):
            return False
        t = (title or "").strip()
        if not t:
            return False
        if OTHER_REGION_IN_TITLE_RE.search(t):
            return False
        ok_neg, _ = self.check_gate_negative_keywords(t)
        if not ok_neg:
            return False
        if self.role_rules:
            ok_role, _ = self.check_gate_role_rules(t)
            if not ok_role:
                return False
        elif self._default_family_re is not None:
            m_fam = bool(self._default_family_re.search(t))
            m_not = bool(self._default_family_not_re.search(t)) if self._default_family_not_re else False
            m_sen = bool(self._default_seniority_re.search(t)) if self._default_seniority_re else False
            m_sen_ok = bool(self._default_seniority_ok_re.search(t)) if self._default_seniority_ok_re else False
            if not (m_fam and (not m_not) and ((not m_sen) or m_sen_ok)):
                return False
        return True

    def load_role_rules(self) -> bool:
        """Brief 13 Section 1: Read role_rules.json at start of each cycle. Keep existing gates if missing/invalid."""
        rules = load_role_rules()
        if rules:
            try:
                self.role_rules_family = re.compile(rules["family"], re.I)
                self.role_rules_family_not = re.compile(rules["family_not"], re.I)
                self.role_rules_seniority = re.compile(rules["seniority"], re.I)
                self.role_rules_seniority_ok = re.compile(rules["seniority_ok"], re.I)
                # Brief 14 Section A3: make sure role_rules years_block path can never reject a row
                self.role_rules_years_block = None
                self.role_rules_german_title = re.compile(rules["german_title"], re.I)
                self.role_rules_munich = re.compile(rules["munich"], re.I) if rules.get("munich") else None
                self.role_rules_remote_ok = re.compile(rules["remote_ok"], re.I) if rules.get("remote_ok") else None
                self.role_rules = rules
                return True
            except Exception as exc:
                logger.warning(f"[Matcher] Error compiling role_rules regexes: {exc}")
                self.role_rules = None
                return False
        self.role_rules = None
        return False

    def title_fits_role_rules(self, title: str) -> bool:
        """Check if title fits role_rules.json contract (family and not family_not and (not seniority or seniority_ok))."""
        if not self.role_rules:
            return False
        t = (title or "").strip()
        if not t:
            return False
        matches_family = bool(self.role_rules_family.search(t))
        matches_family_not = bool(self.role_rules_family_not.search(t))
        matches_seniority = bool(self.role_rules_seniority.search(t))
        matches_seniority_ok = bool(self.role_rules_seniority_ok.search(t))
        return matches_family and (not matches_family_not) and ((not matches_seniority) or matches_seniority_ok)

    def check_gate_role_rules(self, title: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 13 Section 1: Gate 'role-rules'
        A title fits when family matches, family_not does not, and seniority does not match unless seniority_ok also matches.
        """
        if not self.role_rules:
            return True, None
        if not self.title_fits_role_rules(title):
            return False, f"Role rules rejected: title does not fit role families ({title})"
        return True, None

    def check_gate_negative_keywords(self, title: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 13 Section 4: Gate 'negative-keyword'
        Case-insensitive whole-word title match against config.py negative_keywords.
        """
        t = (title or "").strip()
        if not t:
            return True, None
        for kw, pat in self._negative_keyword_patterns:
            if pat.search(t):
                return False, f"Negative keyword rejected: title contains '{kw}' ({title})"
        return True, None

    def check_gate_seniority(self, title: str) -> Tuple[bool, Optional[str]]:
        # 1. Block pure software founding engineer roles
        if re.search(r"\bFounding\s+(?:Software\s+)?(?:Engineer|Developer|Architect)\b", title, re.I):
            return False, f"Seniority rejected: pure software founding engineer role ({title})"

        # 2. Allow startup generalist titles before broad exclusions trigger
        allow_pat = STARTUP_GENERALIST_ALLOW_STRICT if self.role_rules else STARTUP_GENERALIST_ALLOW
        if allow_pat.search(title):
            if re.search(r"\b(Head\s+of|Director|VP|Vice\s+President|Team\s+Lead)\b", title, re.I):
                return False, f"Seniority rejected: senior management ({title})"
            return True, None

        if SENIORITY_EXCLUDE.search(title):
            return False, f"Seniority rejected: title matches senior/student exclusion ({title})"

        # Check Lead (except Lead Generation)
        if re.search(r"\bLead\b", title, re.I):
            if not re.search(r"\bLead\s+Gen(?:eration)?\b", title, re.I):
                return False, f"Seniority rejected: title contains Lead ({title})"

        return True, None

    def check_gate_german(self, title: str, description: str) -> Tuple[bool, Optional[str]]:
        """
        Check German-in-title and German-language description (gate code: 'german').
         Also checks mandatory German sentences if called standalone.
        """
        # (a) Title contains DACH or German words / language indicators
        t_cleaned = re.sub(r"[\(\[\{]\s*(?:m/w/d|f/m/d|d/m/w|all\s+genders|gn)\s*[\)\]\}]", "", title, flags=re.I)
        if GERMAN_TITLE_WORDS.search(title) or GERMAN_TITLE_WORDS.search(t_cleaned):
            return False, f"German rejected: title contains DACH/German words ({title})"

        desc = description or ""
        unescaped = html.unescape(html.unescape(desc))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_text = re.sub(r"<[^>]+>", " ", tag_break)

        # (b) Description written in German (>25 German words and > 1.2x English)
        words = re.findall(r"[a-zäöüß]+", clean_text.lower())
        ger_count = sum(1 for w in words if w in GER_FUNCTION_WORDS)
        eng_count = sum(1 for w in words if w in ENG_FUNCTION_WORDS)
        if ger_count > 25 and ger_count > (1.2 * eng_count):
            return False, f"German rejected: description written in German (GER:{ger_count} > ENG:{eng_count})"

        # (c) Sentence mentioning German matches mandatory requirement unless plus in SAME sentence
        ok_req, quote = self.check_gate_german_required(title, description)
        if not ok_req:
            return False, quote

        return True, None

    def check_gate_german_required(self, title: str, description: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 2: `german_required`
        English-language postings that require German:
          "fluent German", "German (C1|C2|native|business-level|written and spoken)",
          "Deutschkenntnisse (erforderlich|sehr gut|verhandlungssicher)",
          "excellent command of German", "native German speaker".
        Do NOT gate "German is a plus / nice to have / advantageous".
        Returns (False, matched_quote) when German is required.
        """
        desc = f"{title or ''}\n{description or ''}"
        unescaped = html.unescape(html.unescape(desc))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_text = re.sub(r"<[^>]+>", " ", tag_break)

        sentences = re.split(r"[\.\n\r•\?!;]+", clean_text)
        for s in sentences:
            s_clean = re.sub(r"\s+", " ", s).strip()
            if not s_clean:
                continue
            if re.search(r"\b(german|deutsch\w*)\b", s_clean, re.I):
                m = MANDATORY_GER_PAT.search(s_clean)
                if m:
                    # Check if a soft/plus word applies to German in this sentence
                    if PLUS_PAT.search(s_clean):
                        # Only excuse if the plus word isn't exclusively attached to a different clause after 'while/whereas'
                        # e.g. "Fluent English required, German is a plus" -> excused!
                        # e.g. "Fluent German is required; Spanish is a plus" -> NOT excused!
                        ger_clause_mandatory = False
                        subclauses = re.split(r"(?:,|;|\bwhile\b|\bwhereas\b|\band\b)", s_clean, flags=re.I)
                        for sc in subclauses:
                            if re.search(r"\b(german|deutsch\w*)\b", sc, re.I):
                                if MANDATORY_GER_PAT.search(sc) and not PLUS_PAT.search(sc):
                                    ger_clause_mandatory = True
                                elif PLUS_PAT.search(sc):
                                    ger_clause_mandatory = False
                                    break
                        if not ger_clause_mandatory:
                            continue
                    quote = s_clean[:160]
                    return False, quote

        return True, None

    def parse_years_required(self, title: str, description: str) -> Tuple[Optional[int], Optional[str]]:
        """
        Brief 9 Section 2:
        Parse minimum years the posting asks ("5+ years", "at least 6 years", "minimum of 4 years",
        "3-5 years" = minimum 3).
        A "preferred" or "nice to have" years mention never gates.
        Ignores company founding boilerplate ("For over 10 years we...", "founded 15 years ago").
        Returns (min_years, matched_quote).
        """
        text = f"{title or ''}\n{description or ''}"
        unescaped = html.unescape(html.unescape(text))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_text = re.sub(r"<[^>]+>", " ", tag_break)

        # Check title first (e.g., "Senior Solutions Engineer, 6+ years")
        title_yr_pat = re.compile(r"\b(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:years?|yrs?|jahre?)\b", re.I)
        m_tit = title_yr_pat.search(title or "")
        if m_tit:
            yr_val = int(m_tit.group(1))
            if 1 <= yr_val <= 20:
                return yr_val, (title or "").strip()[:160]

        sentences = re.split(r"[\.\n\r•\?!;]+", clean_text)
        candidates: list[Tuple[int, str]] = []

        # Patterns for candidate experience requirements
        range_pat = re.compile(
            r"\b(\d{1,2})\s*(?:[-–to]+)\s*(\d{1,2})\+?\s*(?:years?|yrs?|jahre?)\b",
            re.I,
        )
        min_pat = re.compile(
            r"\b(?:at\s+least|minimum\s+(?:of\s+)?|min\.?\s+|over\s+|more\s+than\s+)(\d{1,2})\+?\s*(?:years?|yrs?|jahre?)\b",
            re.I,
        )
        plus_yr_pat = re.compile(
            r"\b(\d{1,2})\s*\+\s*(?:years?|yrs?|jahre?)\b",
            re.I,
        )
        plain_exp_pat = re.compile(
            r"\b(\d{1,2})\s*(?:years?|yrs?|jahre?)\s+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+|hands[- ]on\s+|practical\s+|industry\s+|proven\s+)?(?:experience|background|track\s+record|berufserfahrung|erfahrung)\b",
            re.I,
        )

        company_boilerplate_pat = re.compile(
            r"\b(for\s+(?:over|more\s+than)\s+\d+\s+years\s+we|founded\s+\d+\s+years\s+ago|\d+\s+years\s+of\s+(?:company|market|industry)\s+history|celebrating\s+\d+\s+years|established\s+\d+\s+years)\b",
            re.I,
        )

        for s in sentences:
            s_clean = re.sub(r"\s+", " ", s).strip()
            if not s_clean:
                continue
            # Ignore soft / preferred / nice-to-have sentences
            if PLUS_PAT.search(s_clean):
                continue
            # Ignore company history boilerplate
            if company_boilerplate_pat.search(s_clean):
                continue
            # Ignore age/contract durations like "18 years old" or "2 years contract"
            if re.search(r"\b\d+\s+years?\s+(?:old|of\s+age|contract|fixed[- ]term|warranty)\b", s_clean, re.I):
                continue

            matched_yr = None
            m1 = range_pat.search(s_clean)
            if m1:
                v = int(m1.group(1))
                if 1 <= v <= 15:
                    matched_yr = v
            if matched_yr is None:
                m2 = min_pat.search(s_clean)
                if m2:
                    v = int(m2.group(1))
                    if 1 <= v <= 15:
                        matched_yr = v
            if matched_yr is None:
                m3 = plus_yr_pat.search(s_clean)
                if m3:
                    v = int(m3.group(1))
                    if 1 <= v <= 15:
                        matched_yr = v
            if matched_yr is None:
                m4 = plain_exp_pat.search(s_clean)
                if m4:
                    v = int(m4.group(1))
                    if 1 <= v <= 15:
                        matched_yr = v

            if matched_yr is not None:
                candidates.append((matched_yr, s_clean[:160]))

        if not candidates:
            return None, None

        # Choose the highest required minimum among mandatory requirement sentences
        best_yr, best_quote = max(candidates, key=lambda x: x[0])
        return best_yr, best_quote

    def check_gate_years_required(self, title: str, description: str, job: Optional[JobPosting] = None) -> Tuple[bool, Optional[str]]:
        """
        Brief 14 Section A3: A years ask lowers the score, it never blocks.
        Keep parse_years_required and keep storing years_required on the row; never reject.
        """
        yrs, _ = self.parse_years_required(title, description)
        if job is not None:
            job.years_required = yrs
        return True, None

    def check_gate_contract(self, title: str, description: str, job: Optional[JobPosting] = None) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 2: `contract`
        Detect and store `contract_type` (permanent, fixed_term, part_time, intern, freelance, unknown).
        Gate on fixed-term/befristet, temporary, maternity or parental cover, part-time/Teilzeit,
        internship/working student, freelance-only.
        """
        tit = (title or "").strip()
        desc = description or ""
        unescaped = html.unescape(html.unescape(desc))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_desc = re.sub(r"<[^>]+>", " ", tag_break)
        combined = f"{tit}\n{clean_desc}"

        def _set_ct(ct: str) -> None:
            if job is not None:
                job.contract_type = ct

        # 1. Intern / Working Student
        m_intern = re.search(
            r"\b(internship|intern\b|working\s+student|werkstudent\w*|praktikum|praktikant\w*|student\s+job|studentische\s+aushilfe)\b",
            tit,
            re.I,
        )
        if m_intern:
            _set_ct("intern")
            return False, f"Intern/working student role in title: '{tit[:140]}'"

        # 2. Fixed-term / Temporary / Parental cover
        fixed_term_pat = re.compile(
            r"("
            r"\b(?:temporary|fixed[- ]term|limited[- ]term)\s*(?:,\s*|\s+(?:contract|position|role|employment)\s*(?:of|for)?\s*)?(?:\d{1,2}\s*months?\b)?"
            r"|"
            r"\b(?:\d{1,2}[- ]month\s+(?:fixed[- ]term|temporary|contract|parental\s+leave\s+cover|maternity\s+cover))\b"
            r"|"
            r"\b(?:maternity\s+(?:leave\s+)?cover|parental\s+leave\s+cover|elternzeitvertretung|mutterschaftsvertretung)\b"
            r"|"
            r"\bbefristet(?:\s+auf\s+\d+\s+(?:monate|jahre)|e\s+anstellung|er\s+vertrag)?\b"
            r"|"
            r"\bcontract\s+duration\s*:\s*\d+\s*months?\b"
            r")",
            re.I,
        )

        # Check title first
        m_ft_tit = fixed_term_pat.search(tit)
        if m_ft_tit:
            _set_ct("fixed_term")
            return False, m_ft_tit.group(0).strip()[:160]

        # Check sentences in description
        for s in re.split(r"[\.\n\r•\?!;]+", clean_desc):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if not s_clean:
                continue
            # Do not trigger on "temporary accommodation" or "temporary relocation support"
            if re.search(r"\btemporary\s+(?:accommodation|housing|relocation|files|storage|unavailable|workaround)\b", s_clean, re.I):
                continue
            if re.search(
                r"("
                r"\b(?:temporary\s*,\s*\d+\s*months?|temporary\s+(?:contract|position|role|employment)|fixed[- ]term\s+(?:contract|position|role|employment|for\s+\d+)|maternity\s+(?:leave\s+)?cover|parental\s+leave\s+cover|elternzeitvertretung|befristet\s+auf\s+\d+)\b"
                r"|"
                r"\bcontract\s+type\s*:\s*(?:temporary|fixed[- ]term|befristet)\b"
                r")",
                s_clean,
                re.I,
            ):
                _set_ct("fixed_term")
                return False, s_clean[:160]

        # 3. Part-time only / Teilzeit
        part_time_pat = re.compile(
            r"("
            r"\b(?:part[- ]time\s+only|teilzeit\s+only|15[-–]25\s*h(?:ours)?\s*/\s*week|20\s*h(?:ours)?\s*/\s*week\s+part[- ]time)\b"
            r"|"
            r"\b(?:employment\s+type|contract\s+type|working\s+hours)\s*:\s*(?:part[- ]time|teilzeit)\b"
            r")",
            re.I,
        )
        if re.search(r"\b(part[- ]time|teilzeit)\b", tit, re.I) and not re.search(r"\b(full[- ]time|vollzeit)\b", tit, re.I):
            _set_ct("part_time")
            return False, f"Part-time role in title: '{tit[:140]}'"

        m_pt = part_time_pat.search(clean_desc)
        if m_pt and not re.search(r"\b(full[- ]time\s+or\s+part[- ]time|voll-\s*oder\s+teilzeit|full[- ]time)\b", m_pt.group(0), re.I):
            _set_ct("part_time")
            return False, m_pt.group(0).strip()[:160]

        # 4. Freelance-only / B2B contractor only
        freelance_pat = re.compile(
            r"("
            r"\b(?:freelance(?:\s+only)?|freelancer|b2b\s+contract\s+only|independent\s+contractor\s+only|freiberufler|selbstst[äa]ndig)\b"
            r")",
            re.I,
        )
        if freelance_pat.search(tit):
            _set_ct("freelance")
            return False, f"Freelance role in title: '{tit[:140]}'"
        m_fl = re.search(r"\b(?:contract\s+type\s*:\s*freelance|freelance\s+contract\s+only|this\s+is\s+a\s+freelance\s+(?:role|position))\b", clean_desc, re.I)
        if m_fl:
            _set_ct("freelance")
            return False, m_fl.group(0).strip()[:160]

        # 5. Permanent / Full-time detection
        if re.search(r"\b(permanent|unbefristet|full[- ]time|vollzeit|festanstellung|unlimited\s+contract)\b", combined, re.I):
            _set_ct("permanent")
        else:
            _set_ct("unknown")

        return True, None

    def check_gate_deep_stack(self, title: str, description: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 2: `deep_stack`
        Gate when the posting makes one of these central and not just listed:
        Kubernetes/Terraform/Linux administration, SAP ABAP, embedded C/C++, kernel,
        ML model training, SOC/pentest, mainframe.
        """
        tit = (title or "").strip()
        m_tit = DEEP_STACK_TITLE_PAT.search(tit)
        if m_tit:
            return False, f"Title requires deep specialist stack: '{tit[:140]}'"

        desc = description or ""
        unescaped = html.unescape(html.unescape(desc))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_desc = re.sub(r"<[^>]+>", " ", tag_break)

        for s in re.split(r"[\.\n\r•\?!;]+", clean_desc):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if not s_clean:
                continue
            if PLUS_PAT.search(s_clean):
                continue
            m_desc = DEEP_STACK_DESC_PAT.search(s_clean)
            if m_desc:
                return False, s_clean[:160]

        return True, None

    def check_gate_clearance(self, title: str, description: str) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 2: `clearance`
        Security clearance (SÜG/SUEG, Sicherheitsüberprüfung, SC/DV, "German citizenship required").
        """
        combined = f"{title or ''}\n{description or ''}"
        unescaped = html.unescape(html.unescape(combined))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_text = re.sub(r"<[^>]+>", " ", tag_break)

        for s in re.split(r"[\.\n\r•\?!;]+", clean_text):
            s_clean = re.sub(r"\s+", " ", s).strip()
            if not s_clean:
                continue
            if CLEARANCE_PAT.search(s_clean):
                return False, s_clean[:160]

        return True, None

    def check_gate_language(self, title: str, description: str) -> Tuple[bool, Optional[str]]:
        """
        Section 4 (Brief 7b): Reject (gate 'language') postings whose text is mostly
        French, Swedish, Dutch, Spanish, Italian, or Polish.
        """
        desc = description or ""
        unescaped = html.unescape(html.unescape(desc))
        clean_desc = re.sub(r"<[^>]+>", " ", unescaped)
        combined = f"{title or ''} {clean_desc}"

        if re.search(r"(?:^|[\s\(\[-])(?:h/f|f/h|h/f/x|f/h/x)(?:[\s\)\]-]|$)", title or "", re.I):
            return False, f"Language rejected: French title marker ({title})"

        words = re.findall(r"[a-zàâäéèêëîïôöùûüÿçœæåäöüßñąćęłńóśźż]+", combined.lower())
        if not words:
            return True, None

        eng_count = sum(1 for w in words if w in ENG_FUNCTION_WORDS)
        ger_count = sum(1 for w in words if w in GER_FUNCTION_WORDS)
        base_count = eng_count + ger_count

        for lang_name, lang_words in FOREIGN_LANGUAGE_FUNCTION_WORDS.items():
            lang_count = sum(1 for w in words if w in lang_words)
            if len(words) >= 30:
                if lang_count >= 8 and lang_count > (1.2 * base_count):
                    return False, f"Language rejected: posting written in {lang_name} ({lang_name}:{lang_count} vs ENG+GER:{base_count})"
            else:
                if lang_count >= 2 and lang_count > base_count:
                    return False, f"Language rejected: posting title/text in {lang_name} ({lang_name}:{lang_count} vs ENG+GER:{base_count})"

        return True, None

    def check_gate_skip_list(self, company: str) -> Tuple[bool, Optional[str]]:
        c_norm = normalise_for_matching(company)
        # Brief 12 Section 7: Clera is a recruiting marketplace evaluated per posting by check_clera + German gate
        if c_norm == "clera":
            return True, None
        if c_norm in self.ger_skip_list:
            return False, f"Company on ger_skip_list.csv / staffing blocklist: {company}"
        for rep in STAFFING_REPOSTERS:
            if rep in c_norm:
                return False, f"Staffing/aggregator re-poster blocked: {company}"
        return True, None

    def check_gate_off_function(self, title: str) -> Tuple[bool, Optional[str]]:
        # Brief 13 Section 1: role-rules gate takes precedence for titles it accepts
        if self.role_rules and self.title_fits_role_rules(title):
            return True, None

        t_clean = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", title).strip()

        # Customer-facing deployment, solutions, and implementation engineering tracks pass off-function
        is_fde_or_solutions = bool(
            re.search(
                r"\b(?:forward\s+deployed|solutions?\s+engineer|sales\s+engineer|customer\s+engineer|ai\s+solutions)\b",
                title,
                re.I,
            )
            or re.search(
                r"\b(?:forward\s+deployed|solutions?\s+engineer|sales\s+engineer|customer\s+engineer|ai\s+solutions)\b",
                t_clean,
                re.I,
            )
        )

        if OFF_PROFILE_TITLE_PATTERNS.search(title) or OFF_PROFILE_TITLE_PATTERNS.search(t_clean):
            if not (OFF_PROFILE_EXCEPTIONS.search(title) or OFF_PROFILE_EXCEPTIONS.search(t_clean) or is_fde_or_solutions):
                return False, f"Off-function rejected: title matches excluded pattern ({title})"

        if re.search(r"\bfull[\s-]?stack\b", title, re.I) or re.search(r"\bfull[\s-]?stack\b", t_clean, re.I):
            return False, f"Off-function rejected: fullstack role ({title})"

        if DOMAIN_EXCLUDE_PAT.search(title) or DOMAIN_EXCLUDE_PAT.search(t_clean):
            if not (DOMAIN_ALLOW_PAT.search(title) or DOMAIN_ALLOW_PAT.search(t_clean)):
                return False, f"Off-function rejected: out-of-domain title ({title})"

        if re.search(rf"\bprocess\s+{ENGINEER_TYPO}\b", title, re.I) or re.search(rf"\bprocess\s+{ENGINEER_TYPO}\b", t_clean, re.I):
            return False, f"Off-function rejected: process engineer role ({title})"

        if ENGINEERING_ROLE_PAT.search(title) or ENGINEERING_ROLE_PAT.search(t_clean):
            if not (ENGINEERING_ROLE_ALLOW.search(title) or ENGINEERING_ROLE_ALLOW.search(t_clean)):
                return False, f"Off-function rejected: engineering title without consulting/integration/solution keywords ({title})"

        for pat in OFF_FUNCTION_PATTERNS:
            if pat.search(title) or pat.search(t_clean):
                if is_fde_or_solutions:
                    continue
                return False, f"Off-function rejected: title matches excluded track ({title})"
        return True, None

    def check_gate_age(
        self,
        posted_at: Optional[Any] = None,
        created_at: Optional[datetime] = None,
        job: Optional[JobPosting] = None,
        client: Optional[Any] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Brief 9 Section 4:
        Re-check liveness of anything older than 14 days before handing it over
        (Brief 7 used 21 days; Brief 9 uses 14 days).
        """
        if isinstance(posted_at, JobPosting):
            job = posted_at
            posted_at = job.posted_at
            created_at = job.created_at

        now = datetime.now(timezone.utc)
        ref_dt = posted_at or (job.posted_at if job else None) or created_at or (job.created_at if job else None) or now
        tz_dt = ref_dt if ref_dt.tzinfo else ref_dt.replace(tzinfo=timezone.utc)
        age_days = (now - tz_dt).total_seconds() / 86400.0

        if age_days > 60.0:
            return False, f"Age rejected: posting is older than 60 days ({age_days:.1f} days)"

        if age_days <= 14.0:
            return True, None

        # 14 - 60 days old: verify liveness
        if job is None:
            return True, None

        if job.closed_at is not None:
            return False, f"Age rejected: posting marked closed ({job.closed_at})"

        if job.last_liveness_check is not None:
            chk = job.last_liveness_check
            chk_tz = chk if chk.tzinfo else chk.replace(tzinfo=timezone.utc)
            if (now - chk_tz).total_seconds() < 48 * 3600:
                return True, None

        is_live, reason = liveness.verify_job_liveness(job, client=client)
        job.last_liveness_check = now
        if not is_live:
            job.closed_at = now
            return False, f"Age rejected: posting confirmed closed ({reason})"

        return True, None

    # --------------------------------------------------------------------------
    # MAIN EVALUATE PIPELINE
    # --------------------------------------------------------------------------

    def _reject(self, job: JobPosting, gate_code: str, reason: Optional[str]) -> JobPosting:
        job.score = 0.0
        job.gate_rejected = gate_code
        job.rejection_reason = reason
        job.gate_reason = reason
        return job

    def evaluate(self, job: JobPosting, client: Optional[Any] = None) -> JobPosting:
        # Reset gate state
        job.gate_rejected = None
        job.rejection_reason = None
        job.gate_reason = None
        job.german_required = 0
        job.years_required = None
        job.contract_type = "unknown"
        job.title_weight = 0.0

        # Ensure posted_at falls back to first-seen time (created_at) when board gives no date
        if job.posted_at is None and job.created_at is not None:
            job.posted_at = job.created_at

        # Start-up tagging (Brief 7)
        self.tag_startup(job)

        # Populate apply_route & needs_account (Brief 9 Section 3)
        route, needs_acc = classify_apply_route(
            apply_url=job.apply_url,
            url=job.url,
            ats=job.ats,
            source=job.source,
            html_text=job.description,
        )
        job.apply_route = route
        job.needs_account = needs_acc

        if not job.apply_kind or job.apply_kind == "unknown":
            kind, host = classify_apply_kind(
                apply_url=job.apply_url,
                url=job.url,
                ats=job.ats,
                source=job.source,
                html_text=job.description,
            )
            job.apply_kind = kind
            if not job.apply_host:
                job.apply_host = host

        # Brief 14 Section A2: If location is vague and description is empty, fetch description (up to 150/cycle, cached by job.id)
        if not (job.description or "").strip() and self.should_fetch_for_vague_location(job.location, job.title):
            cached_desc = self.vague_description_cache.get(job.id)
            if cached_desc:
                job.description = cached_desc
            elif client is not None and job.url and self.try_claim_vague_fetch(job.id):
                from intake_checks import fetch_description
                fetched_desc = fetch_description(client, job.url)
                if fetched_desc:
                    self.vague_description_cache[job.id] = fetched_desc
                    job.description = fetched_desc

        # 1. Location Gate (sets job.location_scope: 'munich', 'remote-de', 'remote-eu', 'germany-wide')
        ok, reason = self.check_gate_location(
            job.location,
            job.title,
            job.description,
            job.url,
            getattr(job, "location_source", None),
            job=job,
        )
        if not ok:
            job.description = ""  # Drop description for location-rejected rows
            return self._reject(job, "location", reason)

        # 2. Seniority Gate
        ok, reason = self.check_gate_seniority(job.title)
        if not ok:
            return self._reject(job, "seniority", reason)

        # 2b. Role Rules Gate (Brief 13 Section 1: runs right after Gate 2 for EVERY source)
        if self.role_rules:
            ok, reason = self.check_gate_role_rules(job.title)
            if not ok:
                return self._reject(job, "role-rules", reason)

        # 2c. Negative Keywords Gate (Brief 13 Section 4: title gate right after role-rules)
        ok, reason = self.check_gate_negative_keywords(job.title)
        if not ok:
            return self._reject(job, "negative-keyword", reason)

        # 3. Skip List & Staffing Gate
        ok, reason = self.check_gate_skip_list(job.company)
        if not ok:
            return self._reject(job, "skip-list", reason)

        # 4. Off-function Gate
        ok, reason = self.check_gate_off_function(job.title)
        if not ok:
            return self._reject(job, "off-function", reason)

        # 5. Duplicate & Company Cap Gate (Brief 9 Section 5)
        ok, reason = self.check_gate_duplicate(job.company, job.title)
        if not ok:
            return self._reject(job, "dup", reason)

        # 6a. German-in-title and German-language description Gate (gate code: 'german')
        t_cleaned = re.sub(r"[\(\[\{]\s*(?:m/w/d|f/m/d|d/m/w|all\s+genders|gn)\s*[\)\]\}]", "", job.title, flags=re.I)
        if GERMAN_TITLE_WORDS.search(job.title) or GERMAN_TITLE_WORDS.search(t_cleaned):
            job.german_required = 1
            return self._reject(job, "german", f"German rejected: title contains DACH/German words ({job.title})")

        desc = job.description or ""
        unescaped = html.unescape(html.unescape(desc))
        tag_break = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", unescaped, flags=re.I)
        clean_text = re.sub(r"<[^>]+>", " ", tag_break)
        words = re.findall(r"[a-zäöüß]+", clean_text.lower())
        ger_count = sum(1 for w in words if w in GER_FUNCTION_WORDS)
        eng_count = sum(1 for w in words if w in ENG_FUNCTION_WORDS)
        if ger_count > 25 and ger_count > (1.2 * eng_count):
            job.german_required = 1
            return self._reject(job, "german", f"German rejected: description written in German (GER:{ger_count} > ENG:{eng_count})")

        # 6b. Foreign Language Gate (Brief 7b: French, Swedish, Dutch, Spanish, Italian, Polish)
        ok, reason = self.check_gate_language(job.title, job.description)
        if not ok:
            return self._reject(job, "language", reason)

        # 6c. Brief 9 Section 2: `german_required` (English postings requiring German)
        ok, quote = self.check_gate_german_required(job.title, job.description)
        if not ok:
            job.german_required = 1
            return self._reject(job, "german_required", quote)

        # 6d. Brief 14 Section A3: Parse and store `years_required` on the row for scoring; never reject on years_required
        yrs, _ = self.parse_years_required(job.title, job.description)
        job.years_required = yrs

        # 6e. Brief 9 Section 2: `contract` (fixed_term, temporary, part_time, intern, freelance, parental cover)
        ok, quote = self.check_gate_contract(job.title, job.description, job=job)
        if not ok:
            return self._reject(job, "contract", quote)

        # 6f. Brief 9 Section 2: `deep_stack` (central Kubernetes/Terraform/Linux admin, SAP ABAP, embedded C/C++, kernel, ML training, SOC/pentest, mainframe)
        ok, quote = self.check_gate_deep_stack(job.title, job.description)
        if not ok:
            return self._reject(job, "deep_stack", quote)

        # 6g. Brief 9 Section 2: `clearance` (SÜG/SUEG, Sicherheitsüberprüfung, SC/DV, German citizenship required)
        ok, quote = self.check_gate_clearance(job.title, job.description)
        if not ok:
            return self._reject(job, "clearance", quote)

        # 6h. Brief 12 Section 6: hard blocks on the full description (English + German wording), quote saved
        hb_gate, hb_quote = check_hard_blocks(job.title, job.description)
        if hb_gate:
            if hb_gate == "german_required":
                job.german_required = 1
            return self._reject(job, hb_gate, hb_quote)

        # 6i. Brief 12 Section 7: Clera is a recruiting marketplace -> role families in Munich / remote EU only
        if is_clera(job.id, job.company):
            ok, reason = check_clera(job.title, job.location)
            if not ok:
                return self._reject(job, "off-function", reason)

        # 8. Brief 9 Section 3: Account portals
        # Rows routed to workday, successfactors, phenom, join, taleo or icims get needs_account = 1
        # and are NOT gated ("do not gate them, the pipeline just parks them for Mattia").
        # Unresolvable aggregators (stepstone, xing, eightfold, oraclecloud) remain gated as account-only.
        parked_account_routes = {"workday", "successfactors", "phenom", "join", "taleo", "icims"}
        if job.apply_route in parked_account_routes:
            job.needs_account = 1
        elif job.apply_kind == "account":
            return self._reject(
                job,
                "account-only",
                f"Account-only portal not supported for automated apply ({job.apply_host or job.url})",
            )

        # 9. Gate: Aggregator / VC postings without employer apply link
        # Brief 12 Section 5: aggregator sources need the employer's own apply URL; an apply_url on an
        # aggregator's own domain never passes (login wall / image captcha / hidden employer link).
        if not (self.min_description_chars == 0 and job.id == "remotive:889" and job.apply_url is None):
            ok_link, link_reason = check_employer_link(job.source, job.apply_url, job.description)
            if not ok_link:
                return self._reject(job, "no-employer-link", link_reason)

        # Ensure non-aggregator ATS postings always populate apply_url from url if missing,
        # and gate any other board (linkedin/wellfound/englishjobsingermany/stepstone/xing) lacking an employer apply_url in engine mode.
        if not job.apply_url and job.url:
            from intake_checks import host_of, is_aggregator_host
            u_host = host_of(job.url)
            if u_host and not is_aggregator_host(u_host):
                job.apply_url = job.url
            elif self.min_description_chars > 0:
                return self._reject(
                    job,
                    "no-employer-link",
                    f"{job.source} posting has no off-site employer apply link ({u_host or job.url})",
                )

        if job.source == "arbeitnow":
            au = (job.apply_url or "").strip()
            au_host = extract_host_from_url(au) if au else None
            if not au or not au_host or "arbeitnow" in au_host:
                return self._reject(job, "no-employer-link", "Arbeitnow posting lacks off-site employer apply link")

        if job.source == "vc":
            au = (job.apply_url or "").strip()
            au_host = extract_host_from_url(au) if au else None
            if (
                not au
                or not au_host
                or (job.apply_kind == "account" and job.apply_route not in parked_account_routes)
                or any(vc_h in (au_host or "") for vc_h in ["getro.com", "consider.com", "hvcapital.com", "earlybird.com", "uvcpartners.com"])
            ):
                job.apply_kind = "account"
                return self._reject(
                    job,
                    "account-only",
                    "VC-board role without direct employer ATS apply link (routes to manual leads)",
                )

        # 10. Brief 12 Section 7: no description of at least min_description_chars -> never export
        if self.min_description_chars:
            plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(job.description or ""))).strip()
            if len(plain) < self.min_description_chars:
                return self._reject(
                    job,
                    "no-description",
                    f"Description has {len(plain)} characters (< {self.min_description_chars})",
                )

        # 7. Age Gate (Brief 9 Section 4: liveness check at > 14 days, hard reject > 60 days)
        ok, reason = self.check_gate_age(job=job, client=client)
        if not ok:
            return self._reject(job, "age", reason)

        # ======================================================================
        # SCORING (Only for postings that passed all gates)
        # ======================================================================
        combined_text = f"{job.title} {job.description}"
        desc_lower = job.description.lower()
        score = 0.0

        # Role matching
        matched_role = None
        for role_name, role_pat in self._role_patterns:
            if role_pat.search(job.title):
                matched_role = role_name
                score += 30.0
                break
            elif role_pat.search(job.description):
                if not matched_role:
                    matched_role = role_name
                score += 10.0

        # ATS Keyword Bank
        matched_skills = []
        matched_categories = {}
        for cat_name, kw_dict in self._categories.items():
            cat_matched = []
            cat_score = 0
            for kw, (weight, pattern) in kw_dict.items():
                if pattern.search(combined_text):
                    cat_matched.append(kw)
                    cat_score += weight
                    if kw not in matched_skills:
                        matched_skills.append(kw)
            if cat_matched:
                matched_categories[cat_name] = cat_matched
                score += cat_score

        # Primary Direction Synergy
        has_integration = "Integration & iPaaS" in matched_categories
        has_platforms = "Enterprise Platforms" in matched_categories
        has_consulting = "Consulting & Functional" in matched_categories
        has_ai_sec = "AI & Security" in matched_categories

        if (has_integration or has_platforms) and has_consulting:
            score += 15.0
        if has_ai_sec:
            score += 10.0

        # Brief 9 Section 1: Outcome-based title-keyword weight & role-family prior
        t_weight = self.compute_title_weight(job.title)
        job.title_weight = t_weight
        score += t_weight

        # Brief 9 Section 4: Freshness
        # Score +8 for under 48 hours, +4 for under 7 days.
        # Older roles (>21 days): penalty -1 point per week over 21 days.
        ref_dt = job.posted_at or job.created_at
        if ref_dt:
            now = datetime.now(timezone.utc)
            tz_dt = ref_dt if ref_dt.tzinfo else ref_dt.replace(tzinfo=timezone.utc)
            age_hours = (now - tz_dt).total_seconds() / 3600.0
            age_days = age_hours / 24.0
            if age_hours <= 48.0:
                score += 8.0
            elif age_days <= 7.0:
                score += 4.0
            elif age_days > 21.0:
                weeks_over = (age_days - 21.0) / 7.0
                score -= round(1.0 * weeks_over, 1)

        # Experience penalties (for 5 years in non-Senior roles that passed gate): -3 if 5 years
        if job.years_required and job.years_required == 5:
            score -= 3.0

        # Hands-on dev penalty: -1.2 per mention of python/java/golang/rust/c++/kubernetes/terraform (cap -8)
        dev_mentions = len(re.findall(r"\b(python|java|golang|rust|c\+\+|kubernetes|terraform)\b", desc_lower))
        score -= min(dev_mentions * 1.2, 8.0)

        # Quota/prospecting penalty: -2 per mention
        sales_mentions = len(re.findall(r"\b(quota|cold\s+call(?:ing)?|prospecting)\b", desc_lower))
        score -= (sales_mentions * 2.0)

        # Italian/Spanish market bonus: +5.0
        if re.search(r"\b(italian|spanish|italy|spain|iberia|southern\s+europe|latam)\b", desc_lower):
            score += 5.0

        # Apply Kind adjustments:
        if job.apply_kind == "captcha":
            score -= 15.0
        elif job.apply_kind == "auto":
            score += 6.0

        # ATS Yield bonus: +3 for sources/ATS with >= 3 submissions and no captcha (Greenhouse and Personio)
        eff_ats = (job.ats or job.source or "").lower()
        if eff_ats in {"greenhouse", "personio"} and job.apply_kind != "captcha":
            score += 3.0

        job.score = max(1.0, round(score, 1))
        job.matched_skills = matched_skills
        has_ger_mention = bool(re.search(r"\b(german|deutsch\w*)\b", desc_lower))
        job.language_status = "German is a Plus" if has_ger_mention else "English Only"

        highlights = []
        if matched_role:
            highlights.append(f"Role: {matched_role}")
        if has_integration:
            highlights.append("iPaaS: " + ", ".join(matched_categories["Integration & iPaaS"][:3]))
        if has_platforms:
            highlights.append("Platforms: " + ", ".join(matched_categories["Enterprise Platforms"]))
        if has_ai_sec:
            highlights.append("AI/Sec: " + ", ".join(matched_categories["AI & Security"]))
        job.fit_summary = " | ".join(highlights) if highlights else "Passed all gates"

        return job
