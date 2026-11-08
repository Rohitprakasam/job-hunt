"""Skill vocabulary, extraction and must-have coverage. No LLM: plain matching against a known list."""

from __future__ import annotations

import re
from functools import lru_cache

from agent.config import Profile

# Common terms for the target fields (sysadmin, security, cloud, dev). Profile skills are added on top.
BASE_SKILLS = """
linux; unix; windows server; active directory; bash; shell scripting; powershell; python; java; javascript;
typescript; go; c; c++; c#; rust; sql; mysql; postgresql; mongodb; redis; nosql; git; github; gitlab;
ci/cd; jenkins; github actions; docker; kubernetes; helm; terraform; ansible; puppet; chef; aws; azure; gcp;
google cloud; cloud computing; vmware; virtualization; hyper-v; kvm; proxmox; networking; tcp/ip; dns; dhcp;
http; tls; ssl; vpn; firewall; iptables; routing; switching; cisco; ccna; load balancing; nginx; apache;
monitoring; prometheus; grafana; nagios; zabbix; elk; elasticsearch; splunk; siem; soc; incident response;
threat hunting; vulnerability assessment; vulnerability management; penetration testing; ethical hacking;
nmap; wireshark; metasploit; burp suite; owasp; kali linux; malware analysis; digital forensics; ids; ips;
edr; xdr; iam; identity and access management; sso; oauth; ldap; kerberos; pki; encryption; cryptography;
security operations; network security; cloud security; application security; devsecops; compliance; iso 27001;
nist; soc 2; gdpr; risk assessment; audit; patch management; backup; disaster recovery; high availability;
troubleshooting; itil; service desk; ticketing; jira; confluence; servicenow; rest api; graphql; microservices;
react; node.js; next.js; django; flask; fastapi; spring boot; html; css; tailwind; machine learning;
deep learning; nlp; llm; data analysis; pandas; numpy; excel; power bi; tableau; selinux; systemd; cron;
rhel; red hat; centos; ubuntu; debian; rhcsa; rhce; ceh; comptia security+; security+; oscp; wifi; wpa2;
wireless security; automation; scripting; agile; scrum
"""

ALIASES = {
    "k8s": "kubernetes",
    "golang": "go",
    "postgres": "postgresql",
    "js": "javascript",
    "ts": "typescript",
    "nodejs": "node.js",
    "node": "node.js",
    "amazon web services": "aws",
    "microsoft azure": "azure",
    "red hat enterprise linux": "rhel",
    "shell": "shell scripting",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
    "pentesting": "penetration testing",
    "pen testing": "penetration testing",
    "vapt": "vulnerability assessment",
    "sec+": "security+",
}

_MUST_START = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*(requirements|required|must[- ]haves?|qualifications|minimum qualifications|"
    r"basic qualifications|what you(?:'ll)? need|you have|who you are|skills required)\b",
    re.IGNORECASE | re.MULTILINE,
)
_MUST_END = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*(nice to have|preferred|bonus|good to have|plus|benefits|what we offer|perks|"
    r"about us|why join)\b",
    re.IGNORECASE | re.MULTILINE,
)
_DROP_GENERIC = {"c", "go"}  # too ambiguous as bare words unless the profile lists them
_AMBIGUOUS_ALIASES = {"js", "ts", "node", "shell"}


def canon(skill: str) -> str:
    s = re.sub(r"\s+", " ", skill.strip().lower())
    return ALIASES.get(s, s)


def _pattern(skill: str) -> re.Pattern[str]:
    escaped = re.escape(skill).replace(r"\ ", r"[\s-]+")
    return re.compile(rf"(?<![\w+#.]){escaped}(?![\w+#])", re.IGNORECASE)


@lru_cache(maxsize=4096)
def _compiled(skill: str) -> re.Pattern[str]:
    return _pattern(skill)


def profile_skills(profile: Profile) -> set[str]:
    found = {canon(s) for items in profile.skills.values() for s in items}
    found |= {canon(s) for b in profile.all_bullets() for s in b.skills}
    found |= {canon(t) for p in profile.projects for t in p.tags}
    found |= {canon(c.name) for c in profile.certifications}
    return {s for s in found if s}


def vocabulary(profile: Profile) -> set[str]:
    base = {canon(s) for s in BASE_SKILLS.replace("\n", " ").split(";") if s.strip()}
    base -= _DROP_GENERIC
    return base | profile_skills(profile) | (set(ALIASES) - _AMBIGUOUS_ALIASES)


def find_skills(text: str, vocab: set[str]) -> set[str]:
    return {canon(skill) for skill in vocab if _compiled(skill).search(text or "")}


def must_have_section(description: str) -> str:
    """The requirements block when the posting has one, else the whole description."""
    start = _MUST_START.search(description or "")
    if not start:
        return description or ""
    rest = description[start.end() :]
    end = _MUST_END.search(rest)
    return rest[: end.start()] if end else rest


def coverage(job_text: str, profile: Profile) -> tuple[float, set[str], set[str]]:
    """(coverage 0..1, must-have skills found in the job, gaps). No skills found -> neutral 0.5."""
    vocab = vocabulary(profile)
    must = find_skills(must_have_section(job_text), vocab)
    if not must:
        must = find_skills(job_text, vocab)
    if not must:
        return 0.5, set(), set()
    have = profile_skills(profile)
    hit = must & have
    # Long wish-lists are normal; matching 5 of the first 8 is already a good sign for a junior role.
    return min(1.0, len(hit) / min(len(must), 8)), must, must - have
