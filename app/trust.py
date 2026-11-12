"""Trust & relevance heuristics shared by discovery and ingestion.

* Middlemen: recruitment / staffing agencies, talent marketplaces and job
  boards posting "on behalf of a client". They are excluded from the company
  registry and their postings are flagged.
* Profile harvesters / scams: postings whose purpose is collecting CVs or
  personal data (evergreen "talent pool" ads, fees, chat-app interviews,
  apply links that leave the employer's own hiring system, ...).
* Category: tech vs non-tech classification from the job title/department.
* Language: explicitly required non-English languages, or non-English JDs.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# ----------------------------------------------------------------- middlemen

AGENCY_NAME_RE = re.compile(
    r"\b(recruit\w*|staffing|headhunt\w*|head[- ]hunt\w*|personnel|manpower|placement|placements|"
    r"talent\s+(?:solutions|partners|acquisition|group|network|agency|hub|bridge|connect\w*|marketplace|cloud|pool)|"
    r"employment\s+(?:agency|services|solutions)|executive\s+search|search\s+(?:group|partners|firm|associates)|"
    r"resourcing|workforce\s+(?:solutions|group|partners)|careers?|jobs?|hiring|job\s*board|"
    r"hr\s+(?:solutions|services|consult\w*)|outsourc\w*|rpo)\b",
    re.I,
)

KNOWN_MIDDLEMEN = {
    # staffing / recruitment groups
    "adecco", "randstad", "robert half", "roberthalf", "hays", "kforce", "teksystems", "insight global", "aerotek",
    "apex systems", "jobot", "cybercoders", "allegis", "kelly services", "manpowergroup", "michael page", "page group",
    "pagegroup", "harvey nash", "nigel frank", "frank recruitment", "tenth revolution", "motion recruitment",
    "modis", "akkodis", "experis", "hirequest", "volt", "collabera", "infojini", "diverse lynx", "mindlance",
    "russell tobin", "beacon hill", "addison group", "vaco", "talentbridge", "jobleads", "remote people",
    # marketplaces, aggregators, freelance / "AI trainer" funnels
    "indeed", "ziprecruiter", "dice", "glassdoor", "monster", "careerbuilder", "lensa", "jooble", "talent.com",
    "jobgether", "jobright", "jobicy", "remotehunter", "simplyhired", "adzuna", "neuvoo", "upwork", "fiverr", "toptal",
    "turing", "crossover", "andela", "braintrust", "a.team", "mercor", "outlier", "dataannotation", "data annotation",
    "remotasks", "scale ai remotasks", "appen", "telus international ai", "clickworker", "alignerr", "hired",
    "wellfound", "otta", "welcome to the jungle", "builtin", "built in", "hackajob", "triplebyte", "vettery",
    "workana", "freelancer", "guru", "peopleperhour", "x-team", "gun.io", "arc.dev", "lemon.io", "proxify",
}

# Strong signals that a *job description* was posted by a middleman for a client.
# (Weaker phrases like "staffing firm" also appear in real employers' "no agencies" notes.)
AGENCY_JD_RE = re.compile(
    r"(on behalf of (?:our|a|one of our) clients?|our client,? (?:is|a|an)\b|we are (?:a|an) (?:leading )?(?:recruitment|staffing|talent|recruiting) (?:agency|firm|company|partner)|"
    r"(?:recruitment|staffing) agency (?:is )?(?:seeking|looking|hiring))",
    re.I,
)

AGENCY_TEXT_RE = re.compile(
    r"(recruitment agency|recruiting agency|recruiting firm|staffing agency|staffing firm|staffing company|"
    r"talent marketplace|placement agency|headhunting|on behalf of (?:our|a|one of our) clients?|our client,? (?:is|a|an)|"
    r"we are (?:a|an) (?:recruitment|staffing|talent)|recruitment and training firm|we connect (?:talented )?candidates)",
    re.I,
)


def is_middleman(name: str, description: str = "", website: str = "") -> str | None:
    """Return a reason if the company looks like a recruiter / job board / marketplace."""
    low = name.lower().strip()
    compact = re.sub(r"[^a-z0-9.]+", " ", low).strip()
    for known in KNOWN_MIDDLEMEN:
        if compact == known or compact.startswith(known + " ") or compact.endswith(" " + known):
            return f"known middleman ({known})"
    host = (urlsplit(website).hostname or "").removeprefix("www.") if website else ""
    for known in KNOWN_MIDDLEMEN:
        stem = known.replace(" ", "")
        if host and (host == stem or host.startswith(stem + ".")):
            return f"known middleman domain ({host})"
    if m := AGENCY_NAME_RE.search(name):
        return f"agency-like name ('{m.group(0)}')"
    if description and (m := AGENCY_TEXT_RE.search(description[:6000])):
        return f"agency-like description ('{m.group(0)}')"
    return None


# ----------------------------------------------------------------- harvesters / scams

# Hosts that are legitimate employer hiring systems (apply links there are fine).
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "recruitee.com",
    "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "icims.com", "jobvite.com", "bamboohr.com", "breezy.hr", "personio.de",
    "personio.com", "teamtailor.com", "taleo.net", "successfactors.com", "oraclecloud.com", "jazzhr.com",
    "applytojob.com", "breezy.hr", "jobs.personio.de", "jobs.personio.com", "rippling.com", "pinpointhq.com", "homerun.co", "factorialhr.com", "comeet.com",
    "dover.com", "gem.com", "ycombinator.com", "workatastartup.com", "paylocity.com", "ukg.com", "ultipro.com",
)
AGGREGATOR_HOSTS = (
    "remotive.com", "remoteok.com", "arbeitnow.com", "arbeitnow.fr", "himalayas.app", "jobs.workable.com",
)
HARVESTER_HOSTS = (
    "jobicy.com", "forms.gle", "docs.google.com/forms", "typeform.com", "jotform.com", "t.me", "telegram.me", "wa.me",
    "whatsapp.com", "bit.ly", "tinyurl.com", "linktr.ee", "lensa.com", "jooble.org", "jobgether.com",
    "jobright.ai", "talent.com", "ziprecruiter.com", "indeed.com", "dataannotation.tech", "outlier.ai",
    "mercor.com", "remotasks.com", "alignerr.com", "crossover.com", "turing.com", "toptal.com",
)

SCAM_TEXT_RE = re.compile(
    r"((?:contact|message|text|reach|interview\w*|chat|apply|send|dm)[^.\n]{0,40}\b(?:via|on|through|at|using)\s+(?:telegram|whatsapp|signal|google hangouts?)\b|"
    r"\b(?:telegram|whatsapp)\s*(?::|@|\+|number|handle|id\b)|interview (?:will be )?(?:conducted )?(?:via|on) (?:chat|text)|"
    r"(?:pay|purchase|buy) (?:for )?(?:your own )?(?:training|equipment|starter kit|software license)|upfront fee|"
    r"registration fee|processing fee|send (?:us )?your (?:ssn|social security|bank (?:account|details)|passport|id card)|"
    r"(?:check|cheque) (?:deposit|to purchase)|reship\w*|package (?:forwarding|reshipping)|"
    r"crypto(?:currency)? (?:payment|wallet) (?:required|needed)|no experience (?:needed|required).{0,60}\$\d{2,3}\s*(?:/|per)\s*h)",
    re.I,
)
_SCAM_HINTS = ("telegram", "whatsapp", "signal", "hangout", "chat", "text", "fee", "ssn", "social security", "bank",
               "passport", "id card", "cheque", "check ", "reship", "package", "crypto", "no experience", "training",
               "equipment", "starter kit", "software license")
# Evergreen postings that only collect profiles ("talent pool", "general application").
TALENT_POOL_RE = re.compile(
    r"\b(talent (?:pool|community|network)|general application|open application|spontaneous application|"
    r"future opportunit(?:y|ies)|expression of interest|evergreen|join our talent|pipeline role|"
    r"don'?t see (?:a|the) (?:role|position|job)|not seeing (?:a|the) role)\b",
    re.I,
)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    full = url.lower()
    h = _host(url)
    return any(h == x or h.endswith("." + x) or ("/" in x and x in full) for x in hosts)


def trust_flags(*, company: str, title: str, description: str, url: str, apply_url: str,
                authoritative: bool, blocked_domains: tuple[str, ...] = (), blocked_companies: tuple[str, ...] = ()) -> list[str]:
    flags: list[str] = []
    link = apply_url or url
    if (reason := is_middleman(company)) is not None:
        flags.append("middleman: " + reason)
    elif any(k in description[:8000].lower() for k in ("client", "agency", "staffing", "recruit")) and (m := AGENCY_JD_RE.search(description[:8000])):
        flags.append(f"middleman: posted for a client ('{m.group(0)}')")
    if blocked_companies and company.lower().strip() in blocked_companies:
        flags.append("blocked company")
    for u in {url, apply_url} - {""}:
        if host_matches(u, HARVESTER_HOSTS) or (blocked_domains and host_matches(u, blocked_domains)):
            flags.append(f"untrusted link ({_host(u)})")
    if not authoritative and apply_url and not host_matches(apply_url, ATS_HOSTS + AGGREGATOR_HOSTS):
        # Aggregator posting whose apply link goes to an unknown site: may still be the
        # company's own careers page, so this is informational, not blocking.
        pass
    if TALENT_POOL_RE.search(title):
        flags.append("talent-pool / general application (profile collection)")
    low = description[:20000].lower()
    if any(k in low for k in _SCAM_HINTS) and (m := SCAM_TEXT_RE.search(description[:20000])):
        flags.append(f"scam signal ('{m.group(0)[:40]}')")
    return flags


def link_rank(url: str, apply_url: str, authoritative: bool) -> int:
    """Lower is better: which copy of a cross-posted role to show."""
    if authoritative:
        return 0
    link = apply_url or url
    if host_matches(link, ATS_HOSTS):
        return 1  # aggregator copy, but applying goes straight to the employer's ATS
    if link and not host_matches(link, AGGREGATOR_HOSTS + HARVESTER_HOSTS):
        return 2  # likely the employer's own careers site
    return 3  # aggregator page only


# ----------------------------------------------------------------- category

_CATS: list[tuple[str, bool, re.Pattern[str]]] = [
    # (category, is_tech, title pattern) -- first match wins; order matters.
    # Software/security QA titles that the non-software pattern below would otherwise
    # claim ("Software Quality Engineer", "Security Validation Engineer") or that no
    # pattern recognises ("QA Automation Lead", "Engineer in Test", "Security Consultant").
    ("security", True, re.compile(r"\b(security (?:validation|test|testing|qa|quality|research|researcher|consultant|assessment|assurance|automation)|offensive security|ethical hack\w*|red team\w*|bug bounty)\b", re.I)),
    ("qa/test", True, re.compile(r"\b(software (?:quality|validation|qa)|qa (?:automation|engineer\w*|tester|test\w*|lead|analyst)|automation (?:qa|test\w*)|(?:software (?:development )?)?engineer in test|test (?:analyst|lead|architect|manager)|testing engineer)\b", re.I)),
    ("engineering (non-software)", False, re.compile(r"\b(mechanical|civil|structural|chemical|process|manufacturing|industrial|electrical|hardware|mechatronics|aerospace|automotive|hvac|piping|geotechnical|environmental|biomedical|materials|packaging|quality|validation|facilities|field|project|sales|commissioning|reliability \(?maintenance)\s+engineer", re.I)),
    ("security", True, re.compile(r"\b(security engineer|appsec|application security|cyber ?security|infosec|penetration|pentest|red team|blue team|soc analyst|security analyst|threat|detection engineer|vulnerability|devsecops|security architect|incident response|grc engineer|cloud security|product security)\b", re.I)),
    ("data/ml", True, re.compile(r"\b(data (?:engineer|scientist|platform|infrastructure|architect)|machine learning|ml engineer|mlops|ai engineer|ai/ml|deep learning|research engineer|research scientist|nlp|computer vision|llm|analytics engineer|applied scientist)\b", re.I)),
    ("devops/sre", True, re.compile(r"\b(devops|dev ops|sre|site reliability|platform engineer|infrastructure engineer|cloud engineer|cloud architect|systems engineer|kubernetes|release engineer|build engineer|production engineer|reliability engineer|network engineer)\b", re.I)),
    ("qa/test", True, re.compile(r"\b(qa engineer|quality assurance engineer|sdet|test automation|automation engineer|software test\w*|test engineer)\b", re.I)),
    ("software", True, re.compile(r"\b(software|developer|programmer|full[- ]?stack|back[- ]?end|front[- ]?end|web engineer|mobile engineer|ios engineer|android engineer|ios developer|android developer|firmware|embedded software|game (?:developer|programmer|engineer)|engineering manager|tech lead|technical lead|member of technical staff|solutions? architect|software architect|api engineer|blockchain engineer|smart contract)\b", re.I)),
    ("it", True, re.compile(r"\b(it (?:support|engineer|administrator|specialist|manager|operations)|systems? administrator|sysadmin|database administrator|dba|helpdesk|help desk|desktop support|technical support engineer|support engineer|salesforce (?:developer|admin\w*)|erp (?:developer|consultant)|sap (?:developer|consultant|basis))\b", re.I)),
    ("product/design", True, re.compile(r"\b(product manager|technical program manager|tpm|product owner|ux|ui designer|product designer|design engineer)\b", re.I)),
    ("healthcare", False, re.compile(r"\b(nurse|nursing|rn|lpn|physician|doctor|medical|clinical|clinician|pharmac\w*|therap\w*|dental|dentist|hygienist|caregiver|patient|surgeon|surgical|radiolog\w*|psychiatr\w*|psycholog\w*|counselor|veterinar\w*|health aide|cna|paramedic|emt|sonograph\w*|phlebotom\w*|optometr\w*|midwife|care assistant|home health)\b", re.I)),
    ("lab/science", False, re.compile(r"\b(lab(?:oratory)? (?:technician|assistant|manager|scientist|associate)|laboratory|chemist|biolog\w*|scientist,? (?:biology|chemistry)|research associate|microbiolog\w*|histolog\w*|assay|bioprocess|formulation|toxicolog\w*|geolog\w*)\b", re.I)),
    ("trades/labour", False, re.compile(r"\b(warehouse|driver|cdl|forklift|electrician|plumber|mechanic|technician|welder|machinist|carpenter|construction|labou?rer|janitor|custodian|cleaner|housekeep\w*|landscap\w*|installer|assembler|operator|picker|packer|loader|mover|courier|delivery|cook|chef|dishwasher|barista|server|bartender|cashier|retail associate|store (?:associate|manager)|security (?:guard|officer)|maintenance|hvac|field service|line worker|production worker|manufacturing associate|surveyor|crew member)\b", re.I)),
    ("finance/accounting", False, re.compile(r"\b(accountant|accounting|bookkeep\w*|accounts (?:payable|receivable)|payroll|tax|audit\w*|controller|treasury|financial (?:analyst|advisor|planner)|finance (?:manager|analyst|associate|director)|billing|credit analyst|actuar\w*|underwrit\w*|loan officer|banker|cfo)\b", re.I)),
    ("advisory/legal", False, re.compile(r"\b(advisor|adviser|consultant(?! engineer)|attorney|lawyer|counsel|paralegal|legal|compliance (?:officer|analyst|manager)|wealth|insurance agent|broker|claims)\b", re.I)),
    ("sales/marketing", False, re.compile(r"\b(sales|account executive|account manager|business development|bdr|sdr|marketing|seo|content (?:writer|marketing)|copywriter|social media|brand|public relations|communications|partnerships|customer success|growth)\b", re.I)),
    ("people/ops", False, re.compile(r"\b(recruiter|talent acquisition|human resources|hr |people (?:partner|operations)|office manager|executive assistant|administrative|receptionist|customer (?:service|support) (?:representative|agent|associate|specialist)|call center|operations (?:associate|coordinator|manager)|teacher|tutor|instructor|translator|interpreter|writer|editor)\b", re.I)),
]
_TECH_FALLBACK = re.compile(r"\b(engineer\w*|developer|architect|technical|technology|scientist|analyst)\b", re.I)


def categorize(title: str, department: str = "") -> tuple[str, bool]:
    for name, tech, pat in _CATS:
        if pat.search(title):
            return name, tech
    if _TECH_FALLBACK.search(title):
        dep = department.lower()
        if re.search(r"engineering|technology|r&d|product|data|it\b|software|platform|infrastructure|security", dep):
            return "software", True
        return "engineering/other", bool(re.search(r"\b(engineer|developer)\b", title, re.I))
    return "other", False


# ----------------------------------------------------------------- language

LANGUAGES = {
    "german": r"german|deutsch", "french": r"french|fran[cç]ais", "spanish": r"spanish|espa[nñ]ol|castilian",
    "portuguese": r"portuguese|portugu[eê]s", "italian": r"italian|italiano", "dutch": r"dutch|flemish|nederlands",
    "polish": r"(?-i:Polish)", "czech": r"czech", "slovak": r"slovak", "hungarian": r"hungarian", "romanian": r"romanian",
    "bulgarian": r"bulgarian", "greek": r"greek", "turkish": r"turkish", "russian": r"russian", "ukrainian": r"ukrainian",
    "swedish": r"swedish", "norwegian": r"norwegian", "danish": r"danish", "finnish": r"finnish", "icelandic": r"icelandic",
    "estonian": r"estonian", "latvian": r"latvian", "lithuanian": r"lithuanian", "serbian": r"serbian", "croatian": r"croatian",
    "slovenian": r"slovenian", "hebrew": r"hebrew", "arabic": r"arabic", "persian": r"persian|farsi", "hindi": r"hindi",
    "urdu": r"urdu", "bengali": r"bengali", "tamil": r"tamil", "telugu": r"telugu", "marathi": r"marathi",
    "chinese": r"chinese|mandarin|cantonese|putonghua", "japanese": r"japanese", "korean": r"korean", "thai": r"thai",
    "vietnamese": r"vietnamese", "indonesian": r"indonesian|bahasa indonesia|bahasa", "malay": r"malay|bahasa melayu|bahasa malaysia",
    "tagalog": r"tagalog|filipino", "swahili": r"swahili", "afrikaans": r"afrikaans", "catalan": r"catalan", "basque": r"basque",
}
# word -> language (single words; looked up per token, which is much faster than a big regex)
_LANG_WORDS: dict[str, str] = {}
for _lang, _pat in LANGUAGES.items():
    for _w in re.sub(r"\(\?-i:|\)", "", _pat).split("|"):
        _w = _w.replace("[cç]", "c").replace("[nñ]", "n").replace("[eê]", "e")
        if " " not in _w:
            _LANG_WORDS[_w.lower()] = _lang
_LANG_WORDS.update({"français": "french", "español": "spanish", "português": "portuguese", "bahasa": "indonesian",
                    "melayu": "malay", "mandarin": "chinese", "cantonese": "chinese", "putonghua": "chinese"})
_CASE_SENSITIVE = {"polish": "Polish"}  # "polish" is also an English verb/noun
_WORD_RE = re.compile(r"[^\W\d_]+")
_REQ_RE = re.compile(
    r"(?:fluen\w*|native|proficien\w*|business[- ]level|professional (?:working )?proficiency|mother tongue|"
    r"bilingual|full professional|excellent|strong|advanced|perfect|working knowledge|command of|"
    r"\b[abc][12]\b|\bjlpt\b|\btopik\b|\bhsk\b|required|must|mandatory|essential|speak\w*|spoken|written and)",
    re.I,
)
_NICE_TO_HAVE = re.compile(r"(nice to have|is a plus|a plus|bonus|preferred but not|advantage|beneficial|desirable|not required)", re.I)
_CLAUSE_SPLIT = re.compile(r"[.;!?\n•]")
# Very small stop-word sets to detect a description written in another language.
_STOP = {
    "english": {"the", "and", "with", "you", "for", "our", "will", "are", "your", "we", "to", "of", "in", "is"},
    "german": {"und", "der", "die", "das", "mit", "wir", "sie", "für", "ist", "eine", "auf", "bei", "zu"},
    "french": {"et", "les", "des", "vous", "nous", "pour", "une", "avec", "dans", "est", "sur", "du"},
    "spanish": {"y", "los", "las", "con", "para", "una", "del", "que", "por", "nuestro", "como", "es"},
    "portuguese": {"e", "os", "com", "para", "uma", "dos", "que", "você", "nosso", "das", "não"},
    "italian": {"e", "il", "di", "che", "per", "una", "con", "del", "della", "sono", "nostro"},
    "dutch": {"en", "het", "de", "van", "een", "met", "voor", "wij", "je", "jij", "zijn", "onze"},
    "polish": {"i", "w", "z", "na", "do", "się", "jest", "dla", "oraz", "nasz"},
}
_WORD = re.compile(r"[a-zà-ÿąćęłńóśźżõãçõé]+", re.I)
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿؀-ۿЀ-ӿ֐-׿]")


def text_language(text: str) -> str:
    sample = text[:4000]
    if not sample.strip():
        return "unknown"
    if len(_CJK.findall(sample)) > 40:
        return "non-latin"
    words = [w.lower() for w in _WORD.findall(sample)]
    if len(words) < 30:
        return "unknown"
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOP.items()}
    best = max(scores, key=scores.get)
    if best != "english" and scores[best] > scores["english"] * 1.3:
        return best
    return "english"


def _language_hits(text: str):
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        lang = _LANG_WORDS.get(w.lower())
        if lang and (w.lower() not in _CASE_SENSITIVE or w == _CASE_SENSITIVE[w.lower()]):
            yield lang, m.start(), m.end()


def required_languages(title: str, description: str) -> list[str]:
    """Non-English languages the posting explicitly requires (title or JD).
    Finds language names via a token scan, then inspects only the clause
    around each hit for requirement wording."""
    found = {lang for lang, _, _ in _language_hits(title)}
    text = description[:30000]
    low = text.lower()
    if not any(w in low for w in _LANG_WORDS):  # fast exit for most JDs
        return sorted(found)
    for lang, a, b in _language_hits(text):
        if lang in found:
            continue
        before = _CLAUSE_SPLIT.split(text[max(0, a - 80):a])[-1]
        after = _CLAUSE_SPLIT.split(text[b:b + 60])[0]
        clause = f"{before}{text[a:b]}{after}"
        if _REQ_RE.search(clause) and not _NICE_TO_HAVE.search(clause):
            found.add(lang)
    return sorted(found)
