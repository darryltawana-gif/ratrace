"""Scam Doctor's offline rules.

The rules are deliberately explainable: every finding tells the user what was
seen and what to do next. They cover English, Twi and common Ghanaian Pidgin,
and they are the free first line of defence before an optional cloud AI check.

A rule is a signal, not a verdict. Normal buying and selling can mention
delivery fees or payment, so a single low finding must not be presented as
proof that someone is a scammer.
"""
import re

WEIGHTS = {"low": 10, "medium": 18, "high": 38}
MAX_SCORE = 100


def phrase(text):
    """Match a whole phrase, allowing any whitespace between words."""
    escaped = re.escape(text).replace(r"\ ", r"\s+")
    return rf"(?<!\w){escaped}(?!\w)"


def anyof(*phrases):
    return "|".join(phrase(p) for p in phrases)


URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.I)
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "rb.gy", "shorturl.at",
    "t.ly", "is.gd", "buff.ly", "ow.ly", "rebrand.ly", "bit.do", "tiny.cc",
    "adf.ly", "shorte.st", "bl.ink", "lnkd.in", "s.id", "v.gd",
}
SUSPICIOUS_TLDS = {
    ".xyz", ".top", ".click", ".link", ".zip", ".mov", ".rest", ".buzz", ".icu",
    ".cam", ".work", ".loan", ".download", ".stream", ".gq", ".tk", ".ml", ".cf",
}


RULES = [
    {
        "code": "urgency",
        "label": "Urgent pressure",
        "severity": "low",
        "detail": "The message pushes you to act immediately.",
        "advice": "Slow down. Real sellers and real banks give you time to verify.",
        "patterns": [
            anyof("urgent", "immediately", "right now", "right away", "act now",
                  "last chance", "today only", "only today", "don't delay",
                  "do not delay", "hurry", "expires today", "final warning",
                  "before it is too late", "within 10 minutes", "within 30 minutes",
                  "within 1 hour", "within 2 hours", "now now", "sharp sharp",
                  "ntɛm", "ntem", "seisei", "yɛ no ntɛm", "ka no ntɛm", "ma yɛn ntɛm"),
            r"within\s+\d+\s*(?:minutes?|hours?)",
        ],
    },
    {
        "code": "secrecy",
        "label": "Asks you to keep it secret",
        "severity": "high",
        "detail": "The message asks you to hide the deal or delete the chat.",
        "advice": "Never keep a payment secret from people you trust. Secrecy is a major scam sign.",
        "patterns": [
            anyof("don't tell anyone", "do not tell anyone", "don't tell anybody",
                  "keep this between us", "keep it secret", "keep it between us",
                  "delete this chat", "delete the chat", "delete the messages",
                  "don't involve anyone", "do not involve anyone", "no one should know",
                  "our secret", "don't report", "do not report", "off the record",
                  "nnka kyerɛ obiara", "yɛ kokoam", "fa sie", "sie no"),
        ],
    },
    {
        "code": "advance_fee",
        "label": "Asks for a fee or top-up first",
        "severity": "high",
        "detail": "You are asked to pay a fee, deposit or top-up before receiving something.",
        "advice": "Do not pay any fee to receive a prize, job, parcel, loan or refund.",
        "patterns": [
            anyof("activation fee", "processing fee", "registration fee",
                  "training fee", "clearance fee", "customs fee", "delivery fee",
                  "courier fee", "service fee", "pickup fee", "shipping fee",
                  "pay the fee", "pay a fee", "send the fee", "send a fee",
                  "pay before delivery", "pay before you receive", "deposit first",
                  "top up", "recharge card", "airtime", "gift card", "itunes card",
                  "steam card", "google play card", "western union", "moneygram",
                  "bitcoin", "usdt", "crypto", "wallet address", "seed phrase",
                  "private key", "tua sika", "tua ka", "fa sika bra", "sika no",
                  "momo no", "ka no", "tua"),
        ],
    },
    {
        "code": "payment_claim",
        "label": "Claims a payment was made",
        "severity": "high",
        "detail": "The message says money has been sent. Screenshots and claims are easy to fake.",
        "advice": "Open your own bank or mobile-money app and confirm the money before releasing goods.",
        "patterns": [
            anyof("i have paid", "i've paid", "i paid", "already paid",
                  "i sent the money", "i have sent the money", "i've sent the money",
                  "payment made", "payment sent", "payment confirmed",
                  "check your momo", "check your mobile money", "check your account",
                  "check your bank", "check your alert", "credit alert", "bank alert",
                  "transaction id", "transaction reference", "reference number",
                  "receipt attached", "screenshot attached", "proof of payment",
                  "matua", "metua", "masoma", "m'asoma", "matua sika no",
                  "masoma sika no"),
        ],
    },
    {
        "code": "overpayment",
        "label": "Overpayment or refund request",
        "severity": "high",
        "detail": "The message claims you were overpaid and asks you to send money back.",
        "advice": "Do not refund anything until your own bank confirms the original payment. Overpayment claims are a common scam.",
        "patterns": [
            anyof("overpaid", "paid too much", "sent too much", "refund the difference",
                  "refund me the balance", "refund the balance", "excess payment",
                  "extra money", "return the balance", "send back the difference",
                  "send the difference", "refund me", "matua pii", "san me sika no",
                  "san ka no", "san me"),
        ],
    },
    {
        "code": "phishing",
        "label": "Phishing or account takeover language",
        "severity": "high",
        "detail": "The message asks you to verify, log in or confirm account details.",
        "advice": "Never log in from a link in a chat. Open the official app yourself and check there.",
        "patterns": [
            anyof("click this link", "click the link", "click here", "verify your account",
                  "verify your momo", "verify your mobile money", "verify your bank",
                  "update your details", "confirm your pin", "confirm your password",
                  "confirm your otp", "enter your pin", "enter your password",
                  "enter your otp", "one-time password", "otp", "login here",
                  "sign in here", "re-enter your", "account will be blocked",
                  "account will be closed", "account has been suspended",
                  "unusual activity", "claim your prize", "you have won", "you've won",
                  "lottery", "congratulations you", "selected winner", "free data",
                  "free airtime", "fa wo pin", "ka wo password", "hyɛ wo account",
                  "wo account no"),
        ],
    },
    {
        "code": "impersonation",
        "label": "Claims to be an official or company",
        "severity": "medium",
        "detail": "Someone claims to be a bank, network, police or support team.",
        "advice": "Call the company using the number on its official website or app. Do not use a number sent in the chat.",
        "patterns": [
            anyof("i am from the bank", "i am from your bank", "i am from mtn",
                  "i am from vodafone", "i am from telecel", "i am from airteltigo",
                  "i am from the police", "i am from government", "i work with the bank",
                  "i work with mtn", "this is the bank", "this is mtn",
                  "customer service", "support team", "official account",
                  "security team", "fraud department", "recovery agent", "admin",
                  "we are from", "your bank", "mefi bank", "mefi mtn", "me yɛ police"),
        ],
    },
    {
        "code": "credentials_request",
        "label": "Asks for PIN, OTP or card details",
        "severity": "high",
        "detail": "The message asks for secrets that no real seller or bank should request.",
        "advice": "Never share your PIN, OTP, password, card number, CVV or mobile-money details with anyone.",
        "patterns": [
            anyof("send your momo number", "send me your momo number", "send your pin",
                  "send me your pin", "send your otp", "send me your otp",
                  "share your otp", "share your pin", "give me your otp",
                  "give me your pin", "what is your pin", "your momo pin",
                  "bank details", "account number", "card number", "cvv",
                  "expiry date", "date of birth", "ghana card", "id number", "bvn",
                  "ssn", "fa wo momo", "ka wo pin", "wo otp", "wo password"),
        ],
    },
    {
        "code": "remote_access",
        "label": "Wants remote access to your phone",
        "severity": "high",
        "detail": "The message asks you to install a remote-control app or share your screen.",
        "advice": "Never install AnyDesk, TeamViewer or a strange APK for a buyer or seller. They can empty your account.",
        "patterns": [
            anyof("anydesk", "teamviewer", "quick support", "screen share",
                  "share your screen", "install this app", "install the app",
                  "download this apk", "download the apk", "apk", "remote access",
                  "access your phone", "chrome remote"),
        ],
    },
    {
        "code": "threats",
        "label": "Threats or blackmail",
        "severity": "high",
        "detail": "The message threatens arrest, court, exposure or account closure.",
        "advice": "Do not pay because of a threat. Keep the messages and report the account to the safety team and the police.",
        "patterns": [
            anyof("you will be arrested", "police will arrest", "court case",
                  "you will be sued", "i will report you", "your account will be blocked",
                  "we will block", "you will lose", "legal action", "deport",
                  "expose you", "post your photos", "send your nudes",
                  "i have your photos", "wɔbɛkyere wo", "wobɛhu", "mebɛka",
                  "police no", "asɛnnii"),
        ],
    },
    {
        "code": "romance",
        "label": "Romance or emotional pressure",
        "severity": "medium",
        "detail": "The message builds a romantic or emotional connection quickly.",
        "advice": "Be careful when someone you have not met says they love you and then asks for money, airtime or gifts.",
        "patterns": [
            anyof("my love", "my darling", "sweetheart", "my dear", "i love you",
                  "i miss you", "our future together", "marry me", "i want to marry",
                  "i will never hurt you", "stuck at the airport", "stuck in hospital",
                  "hospital bill", "visa fee", "flight ticket", "come and meet me",
                  "me dɔ", "ɔdɔ", "me ware wo", "me pɛ wo", "bra ha"),
        ],
    },
    {
        "code": "sob_story",
        "label": "Emergency or sympathy story",
        "severity": "medium",
        "detail": "The message uses an emergency or hardship to ask for help.",
        "advice": "Verify the emergency independently before sending anything. Scammers use urgency and pity.",
        "patterns": [
            anyof("my mother is sick", "my child is sick", "my father is sick",
                  "hospital bill", "i am stranded", "stranded at", "no money for food",
                  "help me with money", "emergency", "accident", "surgery",
                  "my account is blocked", "i lost my phone"),
        ],
    },
    {
        "code": "jobs_investment",
        "label": "Too-good job or investment offer",
        "severity": "high",
        "detail": "The message promises easy money, guaranteed profit or a job for a fee.",
        "advice": "No real job or investment guarantees profit. Never pay a registration or training fee to get work.",
        "patterns": [
            anyof("guaranteed profit", "guaranteed returns", "double your money",
                  "double your investment", "risk-free", "no risk", "100% profit",
                  "daily payout", "weekly payout", "forex", "binary options",
                  "crypto investment", "mining", "ponzi", "susu", "work from home",
                  "passive income", "referral bonus", "downline", "investment plan",
                  "registration fee", "training fee", "earn per day", "earn daily",
                  "nya sika", "yɛ sika", "sika bɛba", "fa wo sika",
                  "investment no"),
            r"earn\s+(?:ghs|gh₵|\$|usd|₵)?\s*\d+",
        ],
    },
    {
        "code": "delivery",
        "label": "Parcel, courier or customs talk",
        "severity": "low",
        "detail": "The message mentions a parcel, courier, customs or delivery payment.",
        "advice": "Track the parcel yourself with the courier's official app. Do not pay a fee from a chat link.",
        "patterns": [
            anyof("parcel", "package", "courier", "dispatch rider", "tracking number",
                  "waybill", "customs clearance", "clearance", "delivery", "address",
                  "pickup", "door delivery", "shipping", "nneɛma no", "de bra",
                  "fa bra", "kɔ fa"),
        ],
    },
    {
        "code": "off_platform",
        "label": "Moves you off the site",
        "severity": "medium",
        "detail": "The message asks to continue on WhatsApp, Telegram or another app.",
        "advice": "Keep the deal and its evidence on The Rat Race. Off-site chats make it harder to report and recover.",
        "patterns": [
            anyof("send me your whatsapp", "send your whatsapp", "let's chat on whatsapp",
                  "chat on whatsapp", "add me on whatsapp", "whatsapp me",
                  "chat me on whatsapp", "telegram", "dm me on instagram",
                  "off the site", "outside the app", "call me on this number"),
        ],
    },
    {
        "code": "trust_pressure",
        "label": "Pushes you to trust them",
        "severity": "medium",
        "detail": "The message uses promises or swearing to lower your guard.",
        "advice": "Trust actions, not words. Verify the person and the payment independently.",
        "patterns": [
            anyof("trust me", "you can trust me", "i swear", "walahi", "wallahi",
                  "believe me", "i promise", "gye me di", "god will bless you",
                  "do me a favour", "do me this favour"),
        ],
    },
    {
        "code": "wrong_number",
        "label": "Unexpected introduction",
        "severity": "low",
        "detail": "Someone you do not know is starting a personal conversation.",
        "advice": "Be cautious with strangers who start personal chats. Do not share private details.",
        "patterns": [
            anyof("wrong number", "is this", "hello dear", "hi dear",
                  "i saw your number", "your friend gave me your number",
                  "are you the one", "do you remember me"),
        ],
    },
    {
        "code": "too_good",
        "label": "Too good to be true",
        "severity": "medium",
        "detail": "The offer sounds free, guaranteed or unusually generous.",
        "advice": "If it sounds too good to be true, stop and verify it. Free money is a common lure.",
        "patterns": [
            anyof("free money", "free gift", "100% free", "guaranteed", "no risk",
                  "double", "bonus", "win big", "prize", "lottery", "jackpot",
                  "free sika", "bonus no", "prize no"),
        ],
    },
]

COMPILED = [
    {**rule, "regexes": [re.compile(p, re.I) for p in rule["patterns"]]}
    for rule in RULES
]


def _link_finding(text):
    urls = URL_RE.findall(text)
    if not urls:
        return None
    hosts = []
    for raw in urls:
        host = raw.lower()
        host = re.sub(r"^https?://", "", host)
        host = re.sub(r"^www\.", "", host)
        host = host.split("/")[0].split("?")[0].split("#")[0]
        if host:
            hosts.append(host)

    bad = []
    severity = "low"
    for host in hosts:
        if host.startswith("xn--") or "xn--" in host:
            bad.append(f"{host} (look-alike domain)")
            severity = "high"
        elif re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?", host):
            bad.append(f"{host} (raw IP address)")
            severity = "high"
        elif any(host == s or host.endswith("." + s) for s in SHORTENERS):
            bad.append(f"{host} (shortened link hides the real address)")
            severity = "high"
        elif any(host.endswith(tld) for tld in SUSPICIOUS_TLDS):
            bad.append(f"{host} (unusual domain ending)")
            if severity != "high":
                severity = "medium"
        else:
            bad.append(host)

    return {
        "code": "link",
        "label": "Link in the message",
        "severity": severity,
        "detail": "Links found: " + ", ".join(dict.fromkeys(bad))[:300],
        "advice": "Do not enter your PIN, OTP or password on a link from a chat. Open the official app instead.",
        "source": "rules",
    }


def analyse_text(text):
    """Return the rules result for a piece of text."""
    text = (text or "").strip()
    flags = []
    if text:
        for rule in COMPILED:
            found = []
            for rx in rule["regexes"]:
                found.extend(m.group(0) for m in rx.finditer(text))
            if found:
                examples = ", ".join(dict.fromkeys(found))[:180]
                flags.append({
                    "code": rule["code"],
                    "label": rule["label"],
                    "severity": rule["severity"],
                    "detail": f"{rule['detail']} Seen: {examples}",
                    "advice": rule["advice"],
                    "source": "rules",
                })
        link = _link_finding(text)
        if link:
            flags.append(link)

    score = min(MAX_SCORE, sum(WEIGHTS.get(f["severity"], 0) for f in flags))
    return finish(score, flags, engine="rules")


def score_flags(flags):
    """Score a list of findings using the same weights as the text rules."""
    return min(MAX_SCORE, sum(WEIGHTS.get(f.get("severity"), 0) for f in flags))


def finish(score, flags, engine="rules", ai_note="", ai_used=False):
    """Turn a score and findings into the shape the UI and database use."""
    score = max(0, min(MAX_SCORE, int(score)))
    if score >= 70:
        risk = "high"
    elif score >= 35:
        risk = "medium"
    elif score >= 10:
        risk = "low"
    else:
        risk = "safe"

    if risk == "high":
        summary = ("This looks like a likely scam. Do not pay, do not share codes, "
                   "and report the account.")
    elif risk == "medium":
        summary = ("Several scam warning signs appeared. Treat this as unsafe until "
                   "you verify it independently.")
    elif risk == "low":
        summary = ("A few caution signs appeared. Read the advice before you pay or "
                   "share anything.")
    else:
        summary = ("We did not find the common scam patterns in this text. That is "
                   "not a guarantee, so still trust your instincts.")

    advice = []
    if risk != "safe":
        advice.append("Stop and check the person through a separate, official channel.")
    advice.extend([
        "Never send money, airtime, gift cards or crypto to someone you have not met.",
        "Never share your PIN, OTP, password, card number or mobile-money details.",
        "Confirm payments inside your own bank or mobile-money app, not from a screenshot.",
        "Keep the conversation and evidence on The Rat Race, and report the account if pressure continues.",
    ])
    for flag in flags:
        tip = flag.get("advice")
        if tip and tip not in advice:
            advice.append(tip)

    return {
        "score": score,
        "risk": risk,
        "flags": flags,
        "summary": summary,
        "advice": advice,
        "engine": engine,
        "ai_used": ai_used,
        "ai_note": ai_note,
    }


def merge(results):
    """Combine several checks without ever lowering the risk of any one."""
    results = [r for r in results if r]
    if not results:
        return finish(0, [])
    score = max(r["score"] for r in results)
    flags = []
    seen = set()
    for result in results:
        for flag in result.get("flags", []):
            key = (flag.get("code"), flag.get("label"))
            if key in seen:
                continue
            seen.add(key)
            flags.append(flag)
    engine = "+".join(dict.fromkeys(r.get("engine", "rules") for r in results))
    ai_used = any(r.get("ai_used") for r in results)
    notes = [r.get("ai_note") for r in results if r.get("ai_note")]
    merged = finish(score, flags, engine=engine, ai_used=ai_used,
                    ai_note=" ".join(dict.fromkeys(notes))[:255])
    # Keep the most serious summary.
    order = {"safe": 0, "low": 1, "medium": 2, "high": 3}
    best = max(results, key=lambda r: order.get(r.get("risk"), 0))
    merged["summary"] = best.get("summary") or merged["summary"]
    return merged
