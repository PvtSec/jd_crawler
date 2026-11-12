"""Skill / requirement extraction from job text.

Each skill has aliases. Plain aliases match case-insensitively on token
boundaries (so "c++", "node.js" and ".net" work); aliases prefixed with
"re:" are raw regular expressions. Patterns are compiled once per taxonomy.
"""
from __future__ import annotations

import re
from difflib import get_close_matches
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig

SKILLS: dict[str, list[str]] = {
    # Languages
    "Python": ["python"],
    "Java": ["java", "jvm"],
    "JavaScript": ["javascript", "js", "ecmascript"],
    "TypeScript": ["typescript"],
    "Go": ["golang", "re:(?-i:\\bGo\\b)(?!-to|\\s+(?:to|through|beyond|live|above|deep|back|forward|after|into|over|for|with|out|on|the|a)\\b)"],
    "Rust": ["rust", "rustlang"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp"],
    "C": ["re:(?-i:\\bC\\b)(?![#+])(?=\\s*(?:/|,|and|or|programming|language))"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Kotlin": ["kotlin"],
    "Swift": ["swiftui", "re:(?-i:\\bSwift\\b)(?!\\s+(?:action|response|decision|resolution|turnaround|pace|delivery|and decisive))"],
    "Objective-C": ["objective-c", "objc"],
    "Scala": ["scala"],
    "Elixir": ["elixir"],
    "Erlang": ["erlang"],
    "Haskell": ["haskell"],
    "Clojure": ["clojure"],
    "Dart": ["dart"],
    "Perl": ["perl"],
    "Lua": ["lua"],
    "R": ["re:(?-i:\\bR\\b)(?=\\s*(?:/|,|and|or|programming|language|studio))", "rstudio"],
    "MATLAB": ["matlab"],
    "Julia": ["re:(?-i:\\bJulia\\b)(?=\\s*(?:/|,|and|or|programming|language))"],
    "Bash": ["bash", "shell scripting", "shell script", "zsh"],
    "PowerShell": ["powershell"],
    "SQL": ["sql"],
    "Solidity": ["solidity"],
    "Zig": ["zig"],
    "Assembly": ["assembly language", "x86 assembly", "arm assembly"],
    # Frontend
    "React": ["react", "react.js", "reactjs"],
    "React Native": ["react native"],
    "Next.js": ["next.js", "nextjs"],
    "Vue": ["vue", "vue.js", "vuejs", "nuxt"],
    "Angular": ["angular", "angularjs"],
    "Svelte": ["svelte", "sveltekit"],
    "HTML/CSS": ["html", "css", "html5", "css3", "sass", "scss"],
    "Tailwind": ["tailwind", "tailwindcss"],
    "Redux": ["redux"],
    "GraphQL": ["graphql", "apollo"],
    "Webpack/Vite": ["webpack", "vite", "esbuild"],
    "Flutter": ["flutter"],
    # Backend frameworks
    "Node.js": ["node.js", "nodejs", "node"],
    "Express": ["express.js", "expressjs"],
    "NestJS": ["nestjs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Spring": ["spring boot", "spring framework", "springboot", "re:(?i:\\bspring\\b)(?=\\s*(?:boot|mvc|cloud|framework|,|/))"],
    "Rails": ["rails", "ruby on rails"],
    ".NET": [".net", "dotnet", "asp.net", ".net core"],
    "Laravel": ["laravel"],
    "gRPC": ["grpc", "protobuf", "protocol buffers"],
    "REST APIs": ["rest api", "restful", "rest apis", "openapi"],
    "Microservices": ["microservices", "microservice", "service-oriented architecture"],
    # Data stores
    "PostgreSQL": ["postgresql", "postgres", "psql"],
    "MySQL": ["mysql", "mariadb"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch", "opensearch", "elastic stack"],
    "Cassandra": ["cassandra", "scylladb"],
    "DynamoDB": ["dynamodb"],
    "SQLite": ["sqlite"],
    "Oracle DB": ["oracle database", "pl/sql", "oracle db"],
    "SQL Server": ["sql server", "mssql", "t-sql"],
    "ClickHouse": ["clickhouse"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery"],
    "Redshift": ["redshift"],
    "Neo4j": ["neo4j", "graph database"],
    # Messaging / data engineering
    "Kafka": ["kafka"],
    "RabbitMQ": ["rabbitmq", "amqp"],
    "Spark": ["spark", "pyspark", "apache spark"],
    "Airflow": ["airflow"],
    "dbt": ["dbt"],
    "Flink": ["flink"],
    "Hadoop": ["hadoop", "hdfs", "hive"],
    "Databricks": ["databricks"],
    "ETL": ["etl", "elt", "data pipelines", "data pipeline"],
    "Pandas": ["pandas", "numpy"],
    # Cloud / infra
    "AWS": ["aws", "amazon web services", "ec2", "s3", "lambda", "ecs", "eks"],
    "GCP": ["gcp", "google cloud", "gke"],
    "Azure": ["azure", "aks"],
    "Docker": ["docker", "containers", "containerization", "dockerfile"],
    "Kubernetes": ["kubernetes", "k8s", "openshift"],
    "Helm": ["helm"],
    "Terraform": ["terraform", "opentofu"],
    "Pulumi": ["pulumi"],
    "Ansible": ["ansible"],
    "Chef/Puppet": ["chef", "puppet", "saltstack"],
    "CloudFormation": ["cloudformation", "aws cdk"],
    "Linux": ["linux", "unix", "ubuntu", "debian", "rhel", "centos"],
    "Nginx": ["nginx", "haproxy", "envoy"],
    "Serverless": ["serverless", "cloud functions"],
    "CI/CD": ["ci/cd", "continuous integration", "continuous delivery", "continuous deployment", "cicd"],
    "GitHub Actions": ["github actions"],
    "Jenkins": ["jenkins"],
    "GitLab CI": ["gitlab ci", "gitlab-ci"],
    "ArgoCD": ["argocd", "argo cd", "gitops", "flux"],
    "Git": ["git"],
    # Observability / SRE
    "Prometheus": ["prometheus"],
    "Grafana": ["grafana"],
    "Datadog": ["datadog"],
    "OpenTelemetry": ["opentelemetry", "otel"],
    "ELK": ["elk", "logstash", "kibana", "splunk"],
    "SRE": ["sre", "site reliability", "on-call", "incident response"],
    "Observability": ["observability", "monitoring and alerting", "monitoring and observability", "apm"],
    # Security
    "Security": ["appsec", "application security", "infosec", "cybersecurity", "cyber security", "security engineering", "product security", "secure coding"],
    "Penetration Testing": ["penetration testing", "pentest", "pen test", "pentesting", "red team"],
    "Cloud Security": ["cloud security", "cspm", "cnapp"],
    "SIEM": ["siem", "soar", "sentinel", "qradar"],
    "IAM": ["iam", "identity and access", "okta", "oauth", "saml", "sso", "oidc"],
    "Threat Detection": ["threat detection", "threat hunting", "detection engineering", "edr", "xdr"],
    "Vulnerability Management": ["vulnerability management", "vulnerability scanning", "cve", "owasp", "sast", "dast"],
    "Network Security": ["network security", "firewall", "firewalls", "ids/ips", "vpn", "zero trust"],
    "Cryptography": ["cryptography", "pki", "encryption", "tls"],
    "Incident Response": ["incident response", "dfir", "forensics"],
    "Compliance": ["soc 2", "soc2", "iso 27001", "pci-dss", "pci dss", "hipaa", "gdpr", "fedramp", "nist"],
    "Reverse Engineering": ["reverse engineering", "malware analysis", "ida pro", "ghidra"],
    # Networking / systems
    "Networking": ["tcp/ip", "bgp", "ospf", "network protocols", "network engineering", "routing and switching"],
    "Distributed Systems": ["distributed systems", "distributed computing", "distributed architecture"],
    "Embedded": ["embedded systems", "embedded software", "embedded linux", "firmware", "rtos", "microcontroller", "microcontrollers"],
    # AI / ML
    "Machine Learning": ["machine learning", "ml", "scikit-learn", "sklearn"],
    "Deep Learning": ["deep learning", "neural networks", "neural network"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow", "keras"],
    "LLM": ["llm", "llms", "large language model", "large language models", "genai", "generative ai", "gpt", "rag", "prompt engineering"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "opencv", "image recognition"],
    "MLOps": ["mlops", "mlflow", "kubeflow", "sagemaker", "vertex ai"],
    "Data Science": ["data science", "statistics", "statistical modeling", "a/b testing"],
    # Mobile
    "iOS": ["ios"],
    "Android": ["android"],
    # Testing / practices
    "Testing": ["unit testing", "integration testing", "tdd", "pytest", "jest", "cypress", "playwright", "selenium", "test automation"],
    "Agile": ["agile", "scrum", "kanban"],
    "System Design": ["system design", "software architecture", "architecture design"],
    # Tools / other
    "Jira": ["jira", "confluence"],
    "Figma": ["figma"],
    "Salesforce": ["salesforce"],
    "SAP": ["sap"],
    "Excel": ["microsoft excel", "re:(?-i:\\bExcel\\b)"],
    "Tableau": ["tableau", "power bi", "looker"],
    "Blockchain": ["blockchain", "web3", "ethereum", "smart contracts"],
    "Unity": ["unity3d", "unity engine", "re:(?-i:\\bUnity\\b)"],
    "Unreal": ["unreal engine", "unreal"],
}

SENIORITY_LEVELS = ["intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director", "executive"]

_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|working student|werkstudent|apprentice|trainee)\b", re.I)),
    ("executive", re.compile(r"\b(vp|vice president|chief|cto|ciso|ceo|cfo|head of)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("manager", re.compile(r"\b(manager|management)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\bstaff\b", re.I)),
    ("lead", re.compile(r"\b(lead|tech lead|team lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|snr)\b|(?-i:\b(III|IV)\b)", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad)\b|(?-i:\bI\b(?![/'’]))", re.I)),
    ("mid", re.compile(r"\b(mid[- ]level|intermediate)\b|(?-i:\bII\b)", re.I)),
]

_YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?:['’]s?)?\b"
    r"(?:\s+of)?(?:\s+\w+){0,6}?\s+(?:experience|exp\b|in\b|working|building|developing)",
    re.I,
)


_TOKEN_RE = re.compile(r"\.?[a-z0-9_+#]+(?:\.[a-z0-9_+#]+)*")


class SkillExtractor:
    def __init__(self, extra: dict[str, list[str]] | None = None):
        taxonomy = dict(SKILLS)
        for name, aliases in (extra or {}).items():
            taxonomy[name] = list(dict.fromkeys(taxonomy.get(name, []) + list(aliases) + [name.lower()]))
        self.names = sorted(taxonomy)
        self._by_lower = {n.lower(): n for n in self.names}
        # Single-token aliases are matched by set lookup against the text's
        # tokens; multi-word aliases by str.find with boundary checks. Only
        # "re:" aliases go through the regex engine.
        self._token: dict[str, set[str]] = {}
        self._phrase: list[tuple[str, tuple[str, ...]]] = []
        regex_parts: list[tuple[str, str]] = []
        for name, aliases in taxonomy.items():
            phrases = []
            for a in aliases:
                if a.startswith("re:"):
                    continue
                a = a.lower()
                if _TOKEN_RE.fullmatch(a):
                    self._token.setdefault(a, set()).add(name)
                else:
                    phrases.append(a)
            if phrases:
                self._phrase.append((name, tuple(phrases)))
            regex_parts += [(name, a[3:]) for a in aliases if a.startswith("re:")]
        # One combined pattern = one pass over the text for all regex skills.
        self._group_names = {f"g{i}": name for i, (name, _) in enumerate(regex_parts)}
        self._regex = re.compile("|".join(f"(?P<g{i}>{rx})" for i, (_, rx) in enumerate(regex_parts))) if regex_parts else None

    @staticmethod
    def _has_phrase(low: str, alias: str) -> bool:
        n = len(alias)
        i = low.find(alias)
        while i != -1:
            before = low[i - 1] if i else " "
            after = low[i + n] if i + n < len(low) else " "
            if not (before.isalnum() or before in "_.") and not (after.isalnum() or after in "_+#"):
                return True
            i = low.find(alias, i + 1)
        return False

    def extract(self, text: str) -> list[str]:
        low = text.lower()
        found: set[str] = set()
        for tok in set(_TOKEN_RE.findall(low)):
            names = self._token.get(tok)
            if names:
                found.update(names)
        found.update(n for n, al in self._phrase if n not in found and any(self._has_phrase(low, a) for a in al))
        if self._regex is not None:
            found.update(self._group_names[m.lastgroup] for m in self._regex.finditer(text))
        return sorted(found)

    def canonical(self, name: str) -> str | None:
        return self._by_lower.get(name.strip().lower())

    def suggest(self, name: str) -> list[str]:
        return [self._by_lower[m] for m in get_close_matches(name.lower(), list(self._by_lower), n=3, cutoff=0.6)]


@lru_cache(maxsize=8)
def _extractor_for(extra_key: tuple[tuple[str, tuple[str, ...]], ...]) -> SkillExtractor:
    return SkillExtractor({k: list(v) for k, v in extra_key})


def get_extractor(cfg: "AppConfig") -> SkillExtractor:
    key = tuple(sorted((k, tuple(v)) for k, v in cfg.skills_extra.items()))
    return _extractor_for(key)


def validate_skill_names(cfg: "AppConfig") -> None:
    """Reject config that references unknown skills (typos would silently match nothing)."""
    for name, aliases in cfg.skills_extra.items():
        for a in aliases:
            if a.startswith("re:"):
                try:
                    re.compile(a[3:])
                except re.error as e:
                    raise ValueError(f"skills_extra '{name}': bad regex {a!r}: {e}") from e
    ex = get_extractor(cfg)
    m = cfg.match
    problems = []
    for field, names in (
        ("skills_required", m.skills_required),
        ("skills_any", m.skills_any),
        ("skills_preferred", list(m.skills_preferred)),
    ):
        for n in names:
            if ex.canonical(n) is None:
                hint = ex.suggest(n)
                problems.append(f"match.{field}: unknown skill '{n}'" + (f" (did you mean {hint}?)" if hint else " (add it under skills_extra)"))
    if problems:
        raise ValueError("; ".join(problems))


def detect_seniority(title: str) -> str:
    for level, pat in _SENIORITY_PATTERNS:
        if pat.search(title):
            return level
    return "mid" if re.search(r"\b(engineer|developer|analyst|scientist|designer)\b", title, re.I) else "unspecified"


def detect_years(text: str) -> int | None:
    """Minimum years of experience demanded: the largest lower bound mentioned."""
    best = None
    for m in _YEARS_RE.finditer(text):
        n = int(m.group(1))
        if 0 < n <= 20:
            best = n if best is None else max(best, n)
    return best


REMOTE_RE = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed team|fully distributed)\b", re.I)
