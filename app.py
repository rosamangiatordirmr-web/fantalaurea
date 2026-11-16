import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# =====================================================================
# 1. CONFIGURAZIONE DELLA PAGINA
# =====================================================================
st.set_page_config(
    page_title="FANTALAUREA",
    page_icon="🎓",
    layout="centered",
)

DATA_FILE = Path(__file__).parent / "data.json"
DEFAULT_ADMIN_PASSWORD = "cambiami-subito"

FESTEGGIATE_DEFAULT = ["DANILA", "MIRIANA", "MARTA", "GIORGIA"]
RULES_DEFAULT = [
    {"desc": "Proclamazione con lode", "points": 50},
    {"desc": "Cade la corona d'alloro", "points": -20},
    {"desc": "Pronuncia la parola 'Ansia' nei primi 5 minuti", "points": 15},
    {"desc": "Commuove almeno un parente stretto", "points": 30},
    {"desc": "Arriva in ritardo alla discussione", "points": -30},
    {"desc": "Festeggiata brilla/allegra prima della proclamazione", "points": 40},
]


# =====================================================================
# 2. INIZIALIZZAZIONE DELLO STATO
# ---------------------------------------------------------------------
# I dati condivisi (squadre, eventi, regolamento, impostazioni) sono
# salvati in data.json, così tutti i partecipanti vedono gli stessi dati
# e nulla si perde ricaricando la pagina.
# st.session_state contiene solo lo stato del singolo browser
# (login admin, squadra che si sta modificando).
# =====================================================================
@st.cache_resource
def _get_lock():
    return threading.Lock()


LOCK = _get_lock()


def default_data():
    return {
        "festeggiate": list(FESTEGGIATE_DEFAULT),
        "rules": [dict(r) for r in RULES_DEFAULT],
        "teams": [],
        "events": [],
        "settings": {
            "iscrizioni_aperte": True,
            "raddoppio_live": True,
            "gioco_concluso": False,
        },
    }


def _read():
    if not DATA_FILE.exists():
        return default_data()
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    base = default_data()
    for key, value in base.items():
        data.setdefault(key, value)
    for key, value in base["settings"].items():
        data["settings"].setdefault(key, value)
    return data


def _write(data):
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def load_data():
    with LOCK:
        return _read()


def update_data(change):
    """Legge, modifica e salva i dati in modo atomico.
    Se `change` solleva ValueError non viene salvato nulla."""
    with LOCK:
        data = _read()
        result = change(data)
        _write(data)
        return result


st.session_state.setdefault("is_admin", False)
st.session_state.setdefault("my_team_id", None)


# ---------------------------------------------------------------------
# Funzioni di supporto
# ---------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%d/%m %H:%M")


def norm(text):
    return " ".join(text.split()).casefold()


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()


def verify_pin(pin, team):
    return hmac.compare_digest(hash_pin(pin, team["salt"]), team["pin_hash"])


def get_admin_password():
    try:
        return st.secrets.get("admin_password")
    except Exception:
        return None


def raddoppio_attivo(data):
    s = data["settings"]
    return s["raddoppio_live"] or s["gioco_concluso"]


def compute_scores(data):
    """Restituisce (punti per festeggiata, classifica ordinata)."""
    double = raddoppio_attivo(data)
    fest_pts = {f: 0 for f in data["festeggiate"]}
    team_bonus = {}
    for e in data["events"]:
        if e["type"] == "festeggiata":
            for f in e["targets"]:
                fest_pts[f] = fest_pts.get(f, 0) + e["points"]
        else:
            team_bonus[e["team_id"]] = team_bonus.get(e["team_id"], 0) + e["points"]

    rows = []
    for t in data["teams"]:
        score = team_bonus.get(t["id"], 0)
        for f in t["festeggiate"]:
            mult = 2 if (double and f == t["captain"]) else 1
            score += fest_pts.get(f, 0) * mult
        rows.append({"team": t, "score": score})

    rows.sort(key=lambda r: (-r["score"], r["team"]["name"].casefold()))
    for r in rows:
        r["pos"] = 1 + sum(1 for o in rows if o["score"] > r["score"])
    return fest_pts, rows


def festeggiate_label(team):
    return ", ".join(f"{f} ⭐" if f == team["captain"] else f for f in team["festeggiate"])


def describe_event(e, teams_by_id):
    if e["type"] == "festeggiata":
        target = " + ".join(e["targets"])
    else:
        team = teams_by_id.get(e["team_id"])
        target = f"Squadra {team['name']}" if team else "(squadra eliminata)"
    return f"**{target}**: {e['desc']} ({e['points']:+d})"


def valida_squadra(data, name, owner, chosen, captain, exclude_id=None):
    errors = []
    if not name.strip():
        errors.append("Inserisci il nome della squadra.")
    if not owner.strip():
        errors.append("Inserisci il tuo nome.")
    if len(chosen) != 2:
        errors.append("Devi scegliere esattamente 2 festeggiate.")
    elif captain not in chosen:
        errors.append("Scegli la capitana tra le 2 festeggiate.")
    others = [t for t in data["teams"] if t["id"] != exclude_id]
    if owner.strip() and any(t["owner_key"] == norm(owner) for t in others):
        errors.append("Hai già creato una squadra: puoi averne una sola. Usa 'Modifica la mia squadra'.")
    if name.strip() and any(norm(t["name"]) == norm(name) for t in others):
        errors.append("Esiste già una squadra con questo nome, scegline un altro.")
    return errors


def scegli_festeggiate(data, key, default_fest=None, default_capt=None):
    chosen = st.multiselect(
        "Scegli esattamente 2 festeggiate:",
        data["festeggiate"],
        default=default_fest or [],
        max_selections=2,
        key=f"{key}_fest",
    )
    captain = None
    if len(chosen) == 2:
        idx = chosen.index(default_capt) if default_capt in chosen else 0
        captain = st.radio(
            "Chi è la tua capitana? (a fine gioco i suoi punti valgono doppio)",
            chosen,
            index=idx,
            horizontal=True,
            key=f"{key}_capt",
        )
    return chosen, captain


# =====================================================================
# 3. MENU LATERALE
# =====================================================================
st.sidebar.title("🎓 Fantalaurea")
pages = ["🏆 Classifica Live", "👥 La tua Squadra", "📜 Regolamento"]
if st.session_state.is_admin:
    pages.append("⚙️ Pannello Admin")
menu = st.sidebar.radio("Vai a:", pages)

st.sidebar.divider()
with st.sidebar.expander("🔐 Area admin", expanded=st.session_state.is_admin):
    if st.session_state.is_admin:
        st.success("Accesso admin attivo")
        if st.button("Esci", key="admin_logout"):
            st.session_state.is_admin = False
            st.rerun()
    else:
        admin_pwd = get_admin_password()
        pwd = st.text_input("Password", type="password", key="admin_pwd_input")
        if st.button("Entra", key="admin_login"):
            if not admin_pwd:
                st.error("Password admin non configurata in .streamlit/secrets.toml")
            elif hmac.compare_digest(pwd.encode(), str(admin_pwd).encode()):
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Password errata.")


# =====================================================================
# 4. CLASSIFICA LIVE (si aggiorna da sola ogni 20 secondi)
# =====================================================================
@st.fragment(run_every=20)
def classifica_live():
    data = load_data()
    fest_pts, rows = compute_scores(data)
    teams_by_id = {t["id"]: t for t in data["teams"]}

    if data["settings"]["gioco_concluso"] and rows:
        winners = [r["team"]["name"] for r in rows if r["pos"] == 1]
        label = "Squadra vincitrice" if len(winners) == 1 else "Squadre vincitrici"
        st.success(f"🥇 **{label}: {', '.join(winners)}** con {rows[0]['score']} punti!")

    if not rows:
        st.info("Ancora nessuna squadra iscritta! Vai su 'La tua Squadra' per registrare la prima.")
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        df = pd.DataFrame(
            [
                {
                    "Pos": f"{medals.get(r['pos'], '')} {r['pos']}".strip(),
                    "Squadra": r["team"]["name"],
                    "Festeggiate": festeggiate_label(r["team"]),
                    "Punti": r["score"],
                }
                for r in rows
            ]
        )
        st.dataframe(df, hide_index=True)
        if raddoppio_attivo(data):
            st.caption("⭐ = capitana, i suoi punti sono già conteggiati doppi.")
        else:
            st.caption("⭐ = capitana, i suoi punti verranno raddoppiati a fine gioco.")

    st.subheader("🎓 Punti delle festeggiate")
    cols = st.columns(len(data["festeggiate"]) or 1)
    for col, f in zip(cols, data["festeggiate"]):
        col.metric(f, fest_pts.get(f, 0))

    st.subheader("📣 Ultimi eventi")
    if not data["events"]:
        st.write("Nessun punto assegnato finora.")
    else:
        for e in reversed(data["events"][-10:]):
            st.markdown(f"`{e['ts']}` {describe_event(e, teams_by_id)}")

    st.caption(f"Ultimo aggiornamento: {datetime.now().strftime('%H:%M:%S')}")


def page_classifica():
    st.title("🏆 Classifica Live")
    st.markdown("Chi salirà sul podio? I punteggi si aggiornano da soli ogni 20 secondi.")
    classifica_live()


# =====================================================================
# 5. CREA / MODIFICA SQUADRA
# =====================================================================
def form_nuova_squadra(data):
    name = st.text_input("Nome della squadra", max_chars=40, key="new_name",
                         placeholder="es. Le cattedratiche")
    owner = st.text_input("Il tuo nome e cognome", max_chars=40, key="new_owner",
                          help="Serve a garantire una sola squadra per persona.")
    pin = st.text_input("Scegli un PIN (4-8 cifre)", type="password", max_chars=8, key="new_pin",
                        help="Ti servirà per modificare la squadra finché le iscrizioni sono aperte.")
    chosen, captain = scegli_festeggiate(data, "new")

    if st.button("Crea squadra 🚀", type="primary"):
        errors = valida_squadra(data, name, owner, chosen, captain)
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            errors.append("Il PIN deve avere da 4 a 8 cifre.")
        if errors:
            for err in errors:
                st.error(err)
            return

        def add(d):
            if not d["settings"]["iscrizioni_aperte"]:
                raise ValueError("Le iscrizioni sono appena state chiuse.")
            errs = valida_squadra(d, name, owner, chosen, captain)
            if errs:
                raise ValueError(errs[0])
            salt = secrets.token_hex(16)
            team = {
                "id": uuid.uuid4().hex,
                "name": name.strip(),
                "owner": owner.strip(),
                "owner_key": norm(owner),
                "salt": salt,
                "pin_hash": hash_pin(pin, salt),
                "festeggiate": chosen,
                "captain": captain,
                "created": now_str(),
                "updated": now_str(),
            }
            d["teams"].append(team)
            return team["id"]

        try:
            team_id = update_data(add)
        except ValueError as ex:
            st.error(str(ex))
        else:
            st.session_state.my_team_id = team_id
            st.success(f"Squadra **{name.strip()}** registrata! Capitana: {captain} ⭐")
            st.balloons()


def form_modifica_squadra(data):
    team = next((t for t in data["teams"] if t["id"] == st.session_state.my_team_id), None)

    if team is None:
        st.session_state.my_team_id = None
        st.markdown("Inserisci il nome con cui hai creato la squadra e il tuo PIN.")
        with st.form("login_team"):
            owner = st.text_input("Il tuo nome e cognome")
            pin = st.text_input("PIN", type="password", max_chars=8)
            if st.form_submit_button("Accedi"):
                t = next((t for t in data["teams"] if t["owner_key"] == norm(owner)), None)
                if t and verify_pin(pin, t):
                    st.session_state.my_team_id = t["id"]
                    st.rerun()
                else:
                    st.error("Nome o PIN non corretti.")
        return

    st.info(f"Stai modificando la squadra di **{team['owner']}**")
    name = st.text_input("Nome della squadra", value=team["name"], max_chars=40,
                         key=f"edit_name_{team['id']}")
    chosen, captain = scegli_festeggiate(data, f"edit_{team['id']}", team["festeggiate"], team["captain"])

    c1, c2 = st.columns(2)
    if c1.button("Salva modifiche 💾", type="primary"):

        def change(d):
            if not d["settings"]["iscrizioni_aperte"]:
                raise ValueError("Le iscrizioni sono state chiuse: non puoi più modificare la squadra.")
            t = next((x for x in d["teams"] if x["id"] == team["id"]), None)
            if t is None:
                raise ValueError("Squadra non trovata.")
            errs = valida_squadra(d, name, t["owner"], chosen, captain, exclude_id=t["id"])
            if errs:
                raise ValueError(errs[0])
            t.update(name=name.strip(), festeggiate=chosen, captain=captain, updated=now_str())

        try:
            update_data(change)
        except ValueError as ex:
            st.error(str(ex))
        else:
            st.success("Squadra aggiornata! ✅")
    if c2.button("Esci"):
        st.session_state.my_team_id = None
        st.rerun()


def page_squadra():
    st.title("👥 La tua Squadra")
    data = load_data()
    if not data["settings"]["iscrizioni_aperte"]:
        st.warning("🔒 Le iscrizioni sono chiuse: le squadre non si possono più creare né modificare. "
                   "Segui la classifica live!")
        return
    st.markdown("Scegli **2 festeggiate** e nomina una **capitana**: a fine gioco i suoi punti valgono doppio. "
                "Puoi creare **una sola squadra** e modificarla finché le iscrizioni restano aperte.")
    tab_new, tab_edit = st.tabs(["✨ Crea squadra", "✏️ Modifica la mia squadra"])
    with tab_new:
        form_nuova_squadra(data)
    with tab_edit:
        form_modifica_squadra(data)


# =====================================================================
# 6. REGOLAMENTO
# =====================================================================
def page_regolamento():
    data = load_data()
    st.title("📜 Regolamento")
    st.markdown(
        f"""
Il Fantalaurea si gioca **dal 1 al 4 dicembre** (inclusi): discussioni, proclamazioni e festa.

**Come si gioca**
1. Crea la tua squadra scegliendo **2 festeggiate** tra {", ".join(data["festeggiate"])}.
2. Nomina una delle due **capitana ⭐**: a fine gioco tutti i suoi punti (bonus e malus) vengono **raddoppiati**.
3. Ogni persona può creare **una sola squadra**, modificabile finché l'admin non chiude le iscrizioni.
4. Ogni volta che una festeggiata fa scattare un bonus o un malus, i punti vanno a tutte le squadre che l'hanno scelta.
5. Al termine della festa vince la squadra con più punti (in caso di parità, vincono a pari merito).

**Bonus e Malus**
"""
    )
    rules_df = pd.DataFrame(
        [{"Evento": r["desc"], "Punti": f"{r['points']:+d}"} for r in data["rules"]]
    )
    if rules_df.empty:
        st.write("Nessuna regola definita.")
    else:
        st.dataframe(rules_df, hide_index=True)
    st.caption("L'admin può assegnare anche punti per eventi speciali non in elenco.")


# =====================================================================
# 7. PANNELLO ADMIN
# =====================================================================
def admin_assegna_punti(data):
    rules = data["rules"]
    teams = data["teams"]
    rule_labels = [f"{r['desc']} ({r['points']:+d})" for r in rules] + ["✍️ Evento personalizzato"]

    target_type = st.radio("Assegna a:", ["Festeggiata/e", "Squadra specifica"], horizontal=True)
    targets, team_id = [], None
    if target_type == "Festeggiata/e":
        targets = st.multiselect("Festeggiata/e protagonista/e:", data["festeggiate"])
    else:
        if not teams:
            st.info("Nessuna squadra iscritta.")
            return
        names = {t["id"]: t["name"] for t in teams}
        team_id = st.selectbox("Squadra:", list(names), format_func=lambda i: names[i])

    choice = st.selectbox("Evento:", range(len(rule_labels)), format_func=lambda i: rule_labels[i])
    if choice == len(rules):
        desc = st.text_input("Descrizione dell'evento")
        pts = st.number_input("Punti (positivi = bonus, negativi = malus)", value=10, step=5)
    else:
        desc, pts = rules[choice]["desc"], rules[choice]["points"]

    if st.button("✅ Registra evento", type="primary"):
        if target_type == "Festeggiata/e" and not targets:
            st.error("Scegli almeno una festeggiata.")
            return
        if not desc.strip():
            st.error("Inserisci una descrizione.")
            return
        event = {
            "id": uuid.uuid4().hex,
            "ts": now_str(),
            "type": "festeggiata" if target_type == "Festeggiata/e" else "squadra",
            "targets": targets,
            "team_id": team_id,
            "desc": desc.strip(),
            "points": int(pts),
        }
        update_data(lambda d: d["events"].append(event))
        st.success(f"Registrato: {event['desc']} ({event['points']:+d})")


def admin_storico(data):
    if not data["events"]:
        st.write("Nessun evento registrato.")
        return
    teams_by_id = {t["id"]: t for t in data["teams"]}
    for e in reversed(data["events"]):
        c1, c2 = st.columns([6, 1])
        c1.markdown(f"`{e['ts']}` {describe_event(e, teams_by_id)}")
        with c2.popover("🗑️"):
            st.write("Annullare questo evento?")
            if st.button("Sì, annulla", key=f"del_ev_{e['id']}"):
                update_data(lambda d: d.update(events=[x for x in d["events"] if x["id"] != e["id"]]))
                st.rerun()


def admin_squadre(data):
    _, rows = compute_scores(data)
    if not rows:
        st.write("Nessuna squadra iscritta.")
        return
    df = pd.DataFrame(
        [
            {
                "Squadra": r["team"]["name"],
                "Creata da": r["team"]["owner"],
                "Festeggiate": festeggiate_label(r["team"]),
                "Punti": r["score"],
                "Ultima modifica": r["team"]["updated"],
            }
            for r in rows
        ]
    )
    st.dataframe(df, hide_index=True)
    st.caption(f"Squadre iscritte: {len(rows)}")

    st.markdown("**Elimina una squadra**")
    names = {t["id"]: f"{t['name']} ({t['owner']})" for t in data["teams"]}
    del_id = st.selectbox("Squadra da eliminare:", list(names), format_func=lambda i: names[i])
    with st.popover("🗑️ Elimina squadra"):
        st.write(f"Eliminare definitivamente **{names[del_id]}**?")
        if st.button("Sì, elimina", key="confirm_del_team"):
            update_data(lambda d: d.update(teams=[t for t in d["teams"] if t["id"] != del_id]))
            st.rerun()


def admin_regolamento(data):
    st.markdown("Modifica, aggiungi o elimina bonus e malus, poi premi **Salva**. "
                "Gli eventi già registrati mantengono i punti originali.")
    df = pd.DataFrame(
        [{"Descrizione": r["desc"], "Punti": r["points"]} for r in data["rules"]],
        columns=["Descrizione", "Punti"],
    )
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        hide_index=True,
        key="rules_editor",
        column_config={
            "Descrizione": st.column_config.TextColumn("Descrizione", required=True),
            "Punti": st.column_config.NumberColumn("Punti", required=True, step=1),
        },
    )
    if st.button("💾 Salva regolamento"):
        new_rules = []
        for _, row in edited.iterrows():
            desc, pts = row["Descrizione"], row["Punti"]
            if pd.isna(desc) or not str(desc).strip():
                continue
            new_rules.append({"desc": str(desc).strip(), "points": 0 if pd.isna(pts) else int(pts)})
        update_data(lambda d: d.update(rules=new_rules))
        st.success(f"Regolamento salvato ({len(new_rules)} regole).")


def admin_impostazioni(data):
    s = data["settings"]
    with st.form("settings_form"):
        aperte = st.toggle("Iscrizioni aperte (creazione e modifica squadre)", value=s["iscrizioni_aperte"])
        raddoppio = st.toggle("Mostra subito il raddoppio della capitana in classifica", value=s["raddoppio_live"],
                              help="Se disattivo, il raddoppio viene applicato quando dichiari il gioco concluso.")
        concluso = st.toggle("Gioco concluso (mostra i vincitori)", value=s["gioco_concluso"])
        if st.form_submit_button("💾 Salva impostazioni"):
            update_data(lambda d: d["settings"].update(
                iscrizioni_aperte=aperte, raddoppio_live=raddoppio, gioco_concluso=concluso))
            st.success("Impostazioni salvate.")


def page_admin():
    if not st.session_state.is_admin:
        st.error("Accesso riservato all'admin.")
        return
    st.title("⚙️ Pannello Admin")
    if get_admin_password() == DEFAULT_ADMIN_PASSWORD:
        st.warning("Stai usando la password di default: cambiala in .streamlit/secrets.toml.")
    data = load_data()
    s = data["settings"]
    st.caption(f"Iscrizioni: {'🟢 aperte' if s['iscrizioni_aperte'] else '🔒 chiuse'} · "
               f"Gioco: {'🏁 concluso' if s['gioco_concluso'] else '▶️ in corso'}")

    tabs = st.tabs(["🎯 Punti", "🧾 Storico", "👥 Squadre", "📜 Regole", "🔧 Impostazioni"])
    with tabs[0]:
        admin_assegna_punti(data)
    with tabs[1]:
        admin_storico(data)
    with tabs[2]:
        admin_squadre(data)
    with tabs[3]:
        admin_regolamento(data)
    with tabs[4]:
        admin_impostazioni(data)


# =====================================================================
# ROUTING DELLE PAGINE
# =====================================================================
if menu == "🏆 Classifica Live":
    page_classifica()
elif menu == "👥 La tua Squadra":
    page_squadra()
elif menu == "📜 Regolamento":
    page_regolamento()
elif menu == "⚙️ Pannello Admin":
    page_admin()
