"""Front Streamlit local du PoC budget.

À lancer impérativement sur la loopback (voir run_app.py ou le README), pour ne
pas exposer l'app au réseau local.

L'app réutilise directement les modules back (store, categorizer, service) dans
le même process : pas d'API HTTP intermédiaire. Les règles sont relues à chaque
interaction, donc toute modification re-catégorise immédiatement l'historique.

Sécurité :
- Pour l'affichage (camembert, transactions, règles), seules la base SQLite et
  rules.json sont lues — tous deux en clair. Aucune passphrase requise.
- La passphrase maître n'est demandée qu'au clic sur « Rafraîchir », utilisée le
  temps de l'appel, puis effacée de l'environnement. Jamais mise en session_state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os

import pandas as pd
import plotly.express as px
import streamlit as st

from budget_poc import config, service
from budget_poc.categorizer import (
    Categorizer,
    CategorizerError,
    load_rules,
    resolve_category,
    save_rules,
    suggest_keyword,
)
from budget_poc.eb_client import EnableBankingError
from budget_poc.secure_store import SecureStoreError
from budget_poc.store import TransactionStore

_PASSPHRASE_ENV = "BUDGET_MASTER_PASSPHRASE"


# --- Chargement des données (mis en cache, invalidé à chaque écriture) --------


@st.cache_data(show_spinner=False)
def _load_dataframe(db_path_str: str, rules_mtime: float, overrides_version: int) -> pd.DataFrame:
    """Charge les transactions catégorisées dans un DataFrame.

    `rules_mtime` et `overrides_version` font partie de la clé de cache : modifier
    les règles ou les corrections manuelles invalide le cache et force la
    recatégorisation.
    """
    settings = config.load_settings()
    db = TransactionStore(settings.db_path)
    categorizer = Categorizer(settings.rules_path)
    overrides = db.get_overrides()
    rows = db.get_transactions()
    records = []
    for txn in rows:
        amount = txn.get("amount", 0.0)
        label = txn.get("label") or ""
        dedup_key = txn.get("dedup_key", "")
        records.append(
            {
                "dedup_key": dedup_key,
                "date": txn.get("value_date") or txn.get("booking_date") or "",
                "montant": amount,
                "devise": txn.get("currency", ""),
                "catégorie": resolve_category(
                    categorizer, overrides, dedup_key, label, amount
                ),
                "manuel": dedup_key in overrides,
                "libellé": label,
            }
        )
    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def _overrides_version(settings: config.Settings) -> int:
    """Compteur de version des overrides (clé d'invalidation du cache)."""
    db = TransactionStore(settings.db_path)
    raw = db.get_meta("overrides_version")
    return int(raw) if raw and raw.isdigit() else 0


def _bump_overrides_version(settings: config.Settings) -> None:
    """Incrémente le compteur de version des overrides après modification."""
    db = TransactionStore(settings.db_path)
    db.set_meta("overrides_version", str(_overrides_version(settings) + 1))


def _rules_mtime(settings: config.Settings) -> float:
    """mtime de rules.json (clé d'invalidation du cache de données)."""
    try:
        return settings.rules_path.stat().st_mtime
    except OSError:
        return 0.0


def _filter_by_period(
    df: pd.DataFrame, period: str, date_range: tuple | None
) -> pd.DataFrame:
    """Filtre le DataFrame selon la période choisie dans la barre latérale.

    `date_range` n'est utilisé qu'en mode « Personnalisée » : un couple
    (début, fin) de dates inclusives.
    """
    if df.empty or period == "Tout":
        return df
    now = pd.Timestamp.now()
    if period == "Mois courant":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return df[df["date"] >= start]
    if period == "3 derniers mois":
        return df[df["date"] >= now - pd.DateOffset(months=3)]
    if period == "Personnalisée" and date_range and len(date_range) == 2:
        start = pd.Timestamp(date_range[0])
        # La date de fin est inclusive : on borne au lendemain à minuit.
        end = pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
        return df[(df["date"] >= start) & (df["date"] < end)]
    # Mode personnalisé incomplet (une seule date sélectionnée) : on ne filtre pas.
    return df


# --- Rafraîchissement (le seul point qui touche l'API et les secrets) --------


@contextlib.contextmanager
def _temporary_passphrase(passphrase: str):
    """Injecte la passphrase dans l'environnement le temps d'un appel, puis l'efface.

    Évite de conserver le secret dans le session_state de Streamlit.
    """
    previous = os.environ.get(_PASSPHRASE_ENV)
    os.environ[_PASSPHRASE_ENV] = passphrase
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_PASSPHRASE_ENV, None)
        else:
            os.environ[_PASSPHRASE_ENV] = previous


def _consent_state(settings: config.Settings) -> tuple[bool, str]:
    """Retourne (session_active, message) à partir de l'expiration en clair."""
    db = TransactionStore(settings.db_path)
    expires_at = service.consent_expires_at(db)
    if expires_at is None:
        return True, "Expiration du consentement inconnue (session ancienne)."
    now = dt.datetime.now(tz=dt.timezone.utc)
    if expires_at <= now:
        return False, "Consentement expiré. Relance `connect` en CLI."
    days = (expires_at - now).days
    return True, f"Consentement valide encore ~{days} jour(s)."


def _render_sidebar(
    settings: config.Settings, df: pd.DataFrame
) -> tuple[str, tuple | None]:
    """Affiche la barre latérale. Retourne (période, plage_de_dates).

    La plage n'est renseignée qu'en mode « Personnalisée ». Le sélecteur de dates
    est borné aux dates réellement présentes dans les données.
    """
    st.sidebar.title("Budget BNP")
    period = st.sidebar.radio(
        "Période",
        ["Mois courant", "3 derniers mois", "Tout", "Personnalisée"],
        index=0,
    )

    date_range: tuple | None = None
    if period == "Personnalisée":
        if df.empty:
            st.sidebar.info("Aucune donnée à filtrer.")
        else:
            min_date = df["date"].min().date()
            max_date = df["date"].max().date()
            # Plage par défaut : mois courant borné aux données disponibles.
            default_start = max(min_date, dt.date.today().replace(day=1))
            selection = st.sidebar.date_input(
                "Du / au",
                value=(default_start, max_date),
                min_value=min_date,
                max_value=max_date,
                format="DD/MM/YYYY",
            )
            # date_input renvoie 1 date pendant la sélection, 2 une fois bouclée.
            if isinstance(selection, (tuple, list)) and len(selection) == 2:
                date_range = (selection[0], selection[1])
            else:
                st.sidebar.caption("Sélectionne la date de fin pour appliquer.")

    st.sidebar.divider()
    st.sidebar.subheader("Rafraîchir")
    active, message = _consent_state(settings)
    st.sidebar.caption(message)

    # Si start.ps1 a déjà chargé BUDGET_MASTER_PASSPHRASE dans l'environnement,
    # on l'utilise telle quelle (évite les erreurs de copier-coller). Sinon, on
    # la demande à la volée (mode strict : passphrase hors du .env).
    env_has_passphrase = bool(os.environ.get(_PASSPHRASE_ENV, "").strip())
    typed_passphrase = None
    if env_has_passphrase:
        st.sidebar.caption("Passphrase déjà chargée depuis l'environnement.")
    else:
        typed_passphrase = st.sidebar.text_input(
            "Passphrase maître", type="password", disabled=not active
        )

    if st.sidebar.button("Rafraîchir maintenant", disabled=not active):
        if env_has_passphrase:
            _run_fetch(settings, None)
        elif not typed_passphrase:
            st.sidebar.error("Saisis la passphrase pour rafraîchir.")
        else:
            _run_fetch(settings, typed_passphrase)
    return period, date_range


def _run_fetch(settings: config.Settings, passphrase: str | None) -> None:
    """Exécute le fetch puis vide le cache.

    Si `passphrase` est None, elle est déjà présente dans l'environnement
    (chargée par start.ps1) et on ne la réécrit pas. Sinon, on l'injecte
    temporairement le temps de l'appel.
    """
    try:
        if passphrase is None:
            with st.spinner("Rafraîchissement…"):
                summary = service.perform_fetch(settings)
        else:
            with _temporary_passphrase(passphrase), st.spinner("Rafraîchissement…"):
                summary = service.perform_fetch(settings)
    except (EnableBankingError, SecureStoreError, RuntimeError) as exc:
        st.sidebar.error(f"Échec : {exc}")
        return
    st.cache_data.clear()
    st.sidebar.success(
        f"+{summary['total_new']} nouvelles, {summary['total_updated']} mises à jour."
    )


# --- Page 1 : Vue d'ensemble (camembert + drill-down) ------------------------


def _page_overview(df: pd.DataFrame) -> None:
    st.header("Vue d'ensemble")
    if df.empty:
        st.info("Aucune transaction sur la période. Rafraîchis ou élargis la période.")
        return

    # On ignore les dates non analysables pour ne pas créer un mois « NaT ».
    df = df[df["date"].notna()].copy()
    df["mois"] = df["date"].dt.strftime("%Y-%m")

    # Conteneur de métriques en haut, rempli une fois le périmètre connu
    # (le périmètre dépend du clic sur le graphique en barres, rendu plus bas).
    metrics_box = st.container()

    debits_all = df[df["montant"] < 0].copy()
    debits_all["dépense"] = -debits_all["montant"]

    col_bar, col_pie = st.columns(2)

    # --- Barres : dépenses par mois (cliquables pour filtrer le reste) ---
    event = None
    with col_bar:
        st.subheader("Dépenses par mois")
        if debits_all.empty:
            st.info("Aucune dépense sur la période.")
        else:
            monthly = (
                debits_all.groupby("mois")["dépense"]
                .sum()
                .reset_index()
                .sort_values("mois")
            )
            fig_bar = px.bar(monthly, x="mois", y="dépense")
            fig_bar.update_layout(
                xaxis_title="", yaxis_title="€", showlegend=False,
                margin={"t": 10, "b": 0, "l": 0, "r": 0},
            )
            # Axe catégoriel : sinon Plotly interprète "2026-06" comme une date et
            # renvoie une date complète au clic, qui ne matche plus la colonne mois.
            fig_bar.update_xaxes(type="category")
            event = st.plotly_chart(
                fig_bar, use_container_width=True, on_select="rerun", key="bar_months"
            )
            st.caption("Clique un mois pour filtrer le camembert et les chiffres.")

    # Mois sélectionné par clic sur une barre (None = toute la période).
    points = (event.get("selection", {}) or {}).get("points", []) if event else []
    selected_month = None
    if points:
        # Défense : on ne garde que AAAA-MM, même si Plotly renvoyait une date complète.
        selected_month = str(points[0].get("x", ""))[:7] or None

    scope = df[df["mois"] == selected_month] if selected_month else df
    scope_label = selected_month if selected_month else "période complète"

    # --- Camembert : répartition des dépenses sur le périmètre courant ---
    scope_debits = scope[scope["montant"] < 0].copy()
    scope_debits["dépense"] = -scope_debits["montant"]
    by_cat = pd.DataFrame(columns=["catégorie", "dépense"])
    with col_pie:
        st.subheader(f"Répartition — {scope_label}")
        if scope_debits.empty:
            st.info("Aucune dépense sur ce périmètre.")
        else:
            by_cat = (
                scope_debits.groupby("catégorie")["dépense"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
            )
            fig_pie = px.pie(by_cat, names="catégorie", values="dépense", hole=0.4)
            fig_pie.update_traces(textposition="inside", textinfo="percent+label")
            st.plotly_chart(fig_pie, use_container_width=True, key="pie")

    # --- Métriques (en haut), recalculées sur le périmètre courant ---
    total_debit = -scope[scope["montant"] < 0]["montant"].sum()
    total_credit = scope[scope["montant"] >= 0]["montant"].sum()
    with metrics_box:
        if selected_month:
            st.caption(f"Mois sélectionné : **{selected_month}** (clique une autre barre pour changer).")
        c1, c2, c3 = st.columns(3)
        c1.metric("Dépenses", f"{total_debit:,.2f} €")
        c2.metric("Recettes", f"{total_credit:,.2f} €")
        c3.metric("Solde net", f"{total_credit - total_debit:,.2f} €")

    # --- Détail d'une catégorie (sur le périmètre courant) ---
    if not by_cat.empty:
        st.divider()
        selected_cat = st.selectbox(
            "Détail d'une catégorie", options=by_cat["catégorie"].tolist()
        )
        if selected_cat:
            detail = scope_debits[scope_debits["catégorie"] == selected_cat].sort_values(
                "date", ascending=False
            )
            st.subheader(f"{selected_cat} — {detail['dépense'].sum():,.2f} €")
            st.dataframe(
                detail[["date", "dépense", "libellé"]],
                use_container_width=True,
                hide_index=True,
            )


# --- Page 2 : Transactions (liste + création de règle) -----------------------


def _page_transactions(df: pd.DataFrame, settings: config.Settings) -> None:
    st.header("Transactions")
    if df.empty:
        st.info("Aucune transaction sur la période.")
        return

    categories = ["(toutes)"] + sorted(df["catégorie"].unique().tolist())
    col1, col2 = st.columns([1, 2])
    chosen = col1.selectbox("Catégorie", categories)
    query = col2.text_input("Recherche dans le libellé").strip().lower()

    view = df
    if chosen != "(toutes)":
        view = view[view["catégorie"] == chosen]
    if query:
        view = view[view["libellé"].str.lower().str.contains(query, na=False)]

    st.caption(f"{len(view)} transaction(s)")

    # Catégories proposables pour une correction manuelle : celles des règles
    # plus celles déjà utilisées, plus "Inconnue".
    rules = load_rules(settings.rules_path)
    categories = sorted({r["category"] for r in rules} | set(df["catégorie"].unique()))

    # Tableau éditable : la colonne "correction" (override) prime sur la règle.
    # Indexé par dedup_key (caché) pour cibler chaque transaction précisément.
    editable = view.copy()
    db = TransactionStore(settings.db_path)
    current_overrides = db.get_overrides()
    editable["correction"] = [
        current_overrides.get(k, "") for k in editable["dedup_key"]
    ]
    editable = editable.set_index("dedup_key").sort_values("date", ascending=False)

    edited = st.data_editor(
        editable[["date", "montant", "catégorie", "manuel", "libellé", "correction"]],
        use_container_width=True,
        hide_index=True,
        disabled=["date", "montant", "catégorie", "manuel", "libellé"],
        column_config={
            "manuel": st.column_config.CheckboxColumn("manuel", help="Corrigé à la main"),
            "correction": st.column_config.SelectboxColumn(
                "correction manuelle",
                help="Force cette transaction dans une catégorie (prime sur les règles). Vide = règle.",
                options=[""] + categories,
            ),
        },
        key="txn_editor",
    )

    if st.button("Enregistrer les corrections manuelles"):
        changes = 0
        for dedup_key, row in edited.iterrows():
            new_value = (row["correction"] or "").strip()
            old_value = current_overrides.get(dedup_key, "")
            if new_value == old_value:
                continue
            if new_value:
                db.set_override(dedup_key, new_value)
            else:
                db.remove_override(dedup_key)
            changes += 1
        if changes:
            _bump_overrides_version(settings)
            st.cache_data.clear()
            st.success(f"{changes} correction(s) enregistrée(s).")
        else:
            st.info("Aucune modification.")

    st.divider()
    st.subheader("Catégoriser un libellé (créer une règle)")
    _rule_creator(view, settings)


def _rule_creator(view: pd.DataFrame, settings: config.Settings) -> None:
    """Mini-formulaire : transforme un libellé en règle (mot-clé pré-rempli)."""
    if view.empty:
        return
    labels = view["libellé"].unique().tolist()
    chosen_label = st.selectbox("Libellé de référence", labels)
    suggested = suggest_keyword(chosen_label)

    keyword = st.text_input("Mot-clé (éditable)", value=suggested).strip()
    category = st.text_input("Catégorie à attribuer").strip()
    rule_type = st.selectbox("Sens", ["any", "debit", "credit"], index=0)

    # Garde-fous sur le mot-clé.
    if keyword:
        df_all = _load_dataframe(str(settings.db_path), _rules_mtime(settings), _overrides_version(settings))
        matches = df_all["libellé"].str.lower().str.contains(
            keyword.lower(), na=False
        ).sum()
        if len(keyword) < 4:
            st.warning("Mot-clé très court : risque de matcher trop large.")
        st.caption(f"Ce mot-clé matche {int(matches)} transaction(s) existante(s).")

    if st.button("Créer la règle"):
        if not keyword or not category:
            st.error("Mot-clé et catégorie sont obligatoires.")
            return
        try:
            rules = load_rules(settings.rules_path)
            # Insertion en TÊTE : priorité maximale (la dernière règle créée gagne).
            rules.insert(
                0,
                {"category": category, "type": rule_type, "keywords": [keyword.lower()]},
            )
            save_rules(settings.rules_path, rules)
        except CategorizerError as exc:
            st.error(f"Règle invalide : {exc}")
            return
        st.cache_data.clear()
        st.success(f"Règle créée : '{keyword}' → {category}. Historique recatégorisé.")


# --- Page 3 : Règles ---------------------------------------------------------


def _page_rules(settings: config.Settings) -> None:
    st.header("Règles de catégorisation")
    st.caption(
        "Liste ordonnée : la première règle qui matche gagne. "
        "Édite, puis enregistre."
    )
    try:
        rules = load_rules(settings.rules_path)
    except CategorizerError as exc:
        st.error(f"rules.json invalide : {exc}")
        return

    rules_df = pd.DataFrame(
        [
            {
                "catégorie": r["category"],
                "sens": r.get("type", "any"),
                "mots-clés": ", ".join(r.get("keywords", [])),
            }
            for r in rules
        ]
    )
    edited = st.data_editor(
        rules_df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "sens": st.column_config.SelectboxColumn(
                options=["any", "debit", "credit"]
            )
        },
    )

    if st.button("Enregistrer les règles"):
        new_rules = []
        for _, row in edited.iterrows():
            category = str(row["catégorie"]).strip()
            if not category:
                continue
            keywords = [
                k.strip().lower() for k in str(row["mots-clés"]).split(",") if k.strip()
            ]
            new_rules.append(
                {
                    "category": category,
                    "type": row["sens"] if row["sens"] in {"any", "debit", "credit"} else "any",
                    "keywords": keywords,
                }
            )
        try:
            save_rules(settings.rules_path, new_rules)
        except CategorizerError as exc:
            st.error(f"Impossible d'enregistrer : {exc}")
            return
        st.cache_data.clear()
        st.success("Règles enregistrées. Historique recatégorisé.")


# --- Page 4 : Projets (sélection manuelle de transactions) -------------------


def _page_projects(df: pd.DataFrame, settings: config.Settings) -> None:
    st.header("Projets")
    db = TransactionStore(settings.db_path)

    with st.expander("➕ Créer un projet"):
        kind = st.radio(
            "Type",
            ["Manuel (je choisis les transactions)", "Budgété (période + enveloppe)"],
            help=(
                "Manuel : tu coches les transactions une à une (ex. bricolage sur "
                "l'année). Budgété : toutes les dépenses d'une période sont incluses "
                "automatiquement, avec un budget prévisionnel (ex. des vacances)."
            ),
        )
        new_name = st.text_input("Nom du projet", key="new_project_name")
        is_budget = kind.startswith("Budgété")
        d_start = d_end = total = None
        if is_budget:
            today = dt.date.today()
            c1, c2 = st.columns(2)
            d_start = c1.date_input("Début", value=today, format="DD/MM/YYYY", key="np_start")
            d_end = c2.date_input(
                "Fin", value=today + dt.timedelta(days=7), format="DD/MM/YYYY", key="np_end"
            )
            total = st.number_input("Budget total (€)", min_value=0.0, step=50.0, value=1000.0)

        if st.button("Créer le projet"):
            if not new_name.strip():
                st.error("Donne un nom au projet.")
            else:
                try:
                    if is_budget:
                        db.create_budget_project(
                            new_name, d_start.isoformat(), d_end.isoformat(), float(total)
                        )
                    else:
                        db.create_project(new_name)
                    st.success(f"Projet « {new_name.strip()} » créé.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("Un projet porte déjà ce nom.")

    projects = db.list_projects()
    if not projects:
        st.info("Aucun projet pour l'instant. Crée-en un ci-dessus.")
        return

    labels = {
        f"{p['name']}  ·  {'budgété' if p['kind'] == 'budget' else 'manuel'}": p["id"]
        for p in projects
    }
    chosen = st.selectbox("Projet", list(labels.keys()))
    project = db.get_project(labels[chosen])
    if project is None:
        return

    if project["kind"] == "budget":
        _render_budget_project(df, settings, db, project)
    else:
        _render_manual_project(df, settings, db, project)


def _render_manual_project(
    df: pd.DataFrame, settings: config.Settings, db: TransactionStore, project: dict
) -> None:
    project_id = project["id"]
    st.caption("Projet manuel : coche les transactions à inclure, laisse le reste décoché.")
    member_keys = db.get_project_transactions(project_id)

    if df.empty:
        st.info("Aucune transaction en base.")
        return

    in_project = df[df["dedup_key"].isin(member_keys)]
    debits = in_project[in_project["montant"] < 0]
    total = -debits["montant"].sum()

    col1, col2, col3 = st.columns(3)
    col1.metric("Total dépensé", f"{total:,.2f} €")
    col2.metric("Recettes", f"{in_project[in_project['montant'] >= 0]['montant'].sum():,.2f} €")
    col3.metric("Transactions", len(in_project))

    if not debits.empty:
        by_cat = (
            (-debits.groupby("catégorie")["montant"].sum())
            .sort_values(ascending=False)
            .reset_index()
        )
        by_cat.columns = ["catégorie", "dépense"]
        fig = px.pie(by_cat, names="catégorie", values="dépense", hole=0.4)
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Ajouter / retirer des transactions")
    min_date = df["date"].min().date()
    max_date = df["date"].max().date()
    col_a, col_b = st.columns(2)
    d_from = col_a.date_input(
        "Du", value=min_date, min_value=min_date, max_value=max_date,
        format="DD/MM/YYYY", key="proj_from",
    )
    d_to = col_b.date_input(
        "Au", value=max_date, min_value=min_date, max_value=max_date,
        format="DD/MM/YYYY", key="proj_to",
    )

    start = pd.Timestamp(d_from)
    end = pd.Timestamp(d_to) + pd.Timedelta(days=1)
    candidates = df[(df["date"] >= start) & (df["date"] < end)].copy()
    candidates["inclure"] = candidates["dedup_key"].isin(member_keys)
    candidates = candidates.set_index("dedup_key").sort_values("date", ascending=False)

    edited = st.data_editor(
        candidates[["inclure", "date", "montant", "catégorie", "libellé"]],
        use_container_width=True,
        hide_index=True,
        disabled=["date", "montant", "catégorie", "libellé"],
        column_config={
            "inclure": st.column_config.CheckboxColumn(
                "dans le projet", help="Coché = inclus dans le projet."
            )
        },
        key=f"proj_editor_{project_id}",
    )

    if st.button("Enregistrer la sélection"):
        changes = 0
        for dedup_key, row in edited.iterrows():
            now_in = bool(row["inclure"])
            was_in = dedup_key in member_keys
            if now_in and not was_in:
                db.add_to_project(project_id, dedup_key)
                changes += 1
            elif not now_in and was_in:
                db.remove_from_project(project_id, dedup_key)
                changes += 1
        st.success(f"{changes} modification(s) enregistrée(s).")
        st.rerun()

    _delete_project_expander(db, project_id)


def _render_budget_project(
    df: pd.DataFrame, settings: config.Settings, db: TransactionStore, project: dict
) -> None:
    pid = project["id"]
    date_start, date_end = project["date_start"], project["date_end"]
    total_budget = project["total_budget"] or 0.0
    st.subheader(project["name"])
    st.caption(
        f"Période : {date_start} → {date_end} · les dépenses de cette période sont "
        "incluses automatiquement (hors exclusions)."
    )

    exclusions = db.get_project_exclusions(pid)
    budgets = db.get_category_budgets(pid)

    # Dépenses réelles de la période, hors exclusions.
    spent_by_cat: dict[str, float] = {}
    spent = 0.0
    period_debits = pd.DataFrame()
    if not df.empty:
        start = pd.Timestamp(date_start)
        end = pd.Timestamp(date_end) + pd.Timedelta(days=1)
        period = df[(df["date"] >= start) & (df["date"] < end)]
        period_debits = period[period["montant"] < 0].copy()
        if not period_debits.empty:
            period_debits["dépense"] = -period_debits["montant"]
            kept = period_debits[~period_debits["dedup_key"].isin(exclusions)]
            spent = kept["dépense"].sum()
            spent_by_cat = kept.groupby("catégorie")["dépense"].sum().to_dict()

    # --- Chiffres clés ---
    remaining = total_budget - spent
    c1, c2, c3 = st.columns(3)
    c1.metric("Enveloppe", f"{total_budget:,.2f} €")
    c2.metric("Dépensé", f"{spent:,.2f} €")
    c3.metric(
        "Reste", f"{remaining:,.2f} €",
        delta=f"{remaining:,.2f} €",
        delta_color="normal" if remaining >= 0 else "inverse",
    )

    # --- Répartition de l'enveloppe par catégorie (éditable) ---
    st.markdown("**Répartition de l'enveloppe**")
    rules = load_rules(settings.rules_path)
    known_cats = sorted(
        {r["category"] for r in rules}
        | (set(df["catégorie"].unique()) if not df.empty else set())
    )
    budget_rows = [{"catégorie": c, "budget (€)": a} for c, a in budgets.items()] or [
        {"catégorie": "", "budget (€)": 0.0}
    ]
    edited_budgets = st.data_editor(
        pd.DataFrame(budget_rows),
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "catégorie": st.column_config.SelectboxColumn(options=known_cats),
            "budget (€)": st.column_config.NumberColumn(
                min_value=0.0, step=10.0, format="%.2f"
            ),
        },
        key=f"budget_editor_{pid}",
    )
    allocated = sum(
        float(r["budget (€)"] or 0)
        for _, r in edited_budgets.iterrows()
        if str(r["catégorie"]).strip()
    )
    to_allocate = total_budget - allocated
    st.caption(
        f"Réparti : {allocated:,.2f} € / {total_budget:,.2f} € — "
        f"{'reste à allouer' if to_allocate >= 0 else 'sur-alloué de'} "
        f"{abs(to_allocate):,.2f} €"
    )
    if st.button("Enregistrer la répartition", key=f"save_budgets_{pid}"):
        new_budgets = {
            str(r["catégorie"]).strip(): float(r["budget (€)"] or 0)
            for _, r in edited_budgets.iterrows()
            if str(r["catégorie"]).strip() and float(r["budget (€)"] or 0) > 0
        }
        db.set_category_budgets(pid, new_budgets)
        st.success("Répartition enregistrée.")
        st.rerun()

    # --- Suivi prévu / réel ---
    st.markdown("**Suivi prévu / réel**")
    all_cats = set(budgets) | set(spent_by_cat)
    if not all_cats:
        st.caption("Pas encore de budget réparti ni de dépense sur la période.")
    else:
        suivi = []
        for cat in sorted(all_cats):
            budget = budgets.get(cat, 0.0)
            real = spent_by_cat.get(cat, 0.0)
            suivi.append(
                {
                    "catégorie": cat,
                    "budget (€)": budget,
                    "dépensé (€)": real,
                    "reste (€)": budget - real,
                    "consommé": (real / budget) if budget > 0 else 0.0,
                    "statut": (
                        "hors budget" if budget == 0
                        else "dépassé" if real > budget
                        else "ok"
                    ),
                }
            )
        suivi_df = pd.DataFrame(suivi)
        st.dataframe(
            suivi_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "budget (€)": st.column_config.NumberColumn(format="%.2f"),
                "dépensé (€)": st.column_config.NumberColumn(format="%.2f"),
                "reste (€)": st.column_config.NumberColumn(format="%.2f"),
                "consommé": st.column_config.ProgressColumn(
                    min_value=0.0, max_value=1.0, format="%.0f%%"
                ),
            },
        )
        st.caption(
            "« hors budget » = catégorie dépensée sans budget prévu. "
            "Le pourcentage peut dépasser 100 % (barre pleine) en cas de dépassement."
        )

    # --- Exclure des transactions parasites (opt-out) ---
    st.divider()
    st.subheader("Exclure des transactions parasites")
    if period_debits.empty:
        st.caption("Aucune dépense sur la période.")
    else:
        table = period_debits.copy()
        table["exclure"] = table["dedup_key"].isin(exclusions)
        table = table.set_index("dedup_key").sort_values("date", ascending=False)
        edited_excl = st.data_editor(
            table[["exclure", "date", "dépense", "catégorie", "libellé"]],
            use_container_width=True,
            hide_index=True,
            disabled=["date", "dépense", "catégorie", "libellé"],
            column_config={
                "exclure": st.column_config.CheckboxColumn(
                    "exclure", help="Coché = retirée du projet (transaction parasite)."
                )
            },
            key=f"excl_editor_{pid}",
        )
        if st.button("Enregistrer les exclusions", key=f"save_excl_{pid}"):
            changes = 0
            for dedup_key, row in edited_excl.iterrows():
                now_ex = bool(row["exclure"])
                was_ex = dedup_key in exclusions
                if now_ex and not was_ex:
                    db.add_exclusion(pid, dedup_key)
                    changes += 1
                elif not now_ex and was_ex:
                    db.remove_exclusion(pid, dedup_key)
                    changes += 1
            st.success(f"{changes} modification(s) enregistrée(s).")
            st.rerun()

    # --- Modifier la période / l'enveloppe ---
    with st.expander("⚙ Modifier la période / l'enveloppe"):
        e1, e2 = st.columns(2)
        ns = e1.date_input(
            "Début", value=dt.date.fromisoformat(date_start),
            format="DD/MM/YYYY", key=f"edit_start_{pid}",
        )
        ne = e2.date_input(
            "Fin", value=dt.date.fromisoformat(date_end),
            format="DD/MM/YYYY", key=f"edit_end_{pid}",
        )
        nb = st.number_input(
            "Budget total (€)", min_value=0.0, step=50.0,
            value=float(total_budget), key=f"edit_budget_{pid}",
        )
        if st.button("Mettre à jour", key=f"update_meta_{pid}"):
            try:
                db.update_budget_project(pid, ns.isoformat(), ne.isoformat(), float(nb))
                st.success("Projet mis à jour.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    _delete_project_expander(db, pid)


def _delete_project_expander(db: TransactionStore, project_id: int) -> None:
    """Encart de suppression d'un projet (commun aux deux types)."""
    with st.expander("🗑 Supprimer ce projet"):
        st.caption("Supprime le projet et ses associations. Les transactions, elles, restent.")
        if st.button("Supprimer définitivement", type="secondary", key=f"del_{project_id}"):
            db.delete_project(project_id)
            st.success("Projet supprimé.")
            st.rerun()


# --- Point d'entrée ----------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Budget BNP", layout="wide")
    # Touche cosmétique : encadre les cartes de chiffres dans les tons de marque.
    # Cible un data-testid stable ; purement décoratif (sans incidence fonctionnelle).
    st.markdown(
        """
        <style>
        [data-testid="stMetric"] {
            background-color: #F2E6D8;
            border: 1px solid #8C5B3E;
            border-radius: 10px;
            padding: 12px 16px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    try:
        settings = config.load_settings()
    except config.ConfigError as exc:
        st.error(f"Configuration invalide : {exc}")
        st.stop()

    # On charge les données AVANT la barre latérale pour pouvoir borner le
    # sélecteur de dates personnalisé aux dates réellement présentes.
    df_full = _load_dataframe(
        str(settings.db_path), _rules_mtime(settings), _overrides_version(settings)
    )
    period, date_range = _render_sidebar(settings, df_full)
    df = _filter_by_period(df_full, period, date_range)

    page = st.sidebar.radio(
        "Page", ["Vue d'ensemble", "Transactions", "Règles", "Projets"], index=0
    )
    if page == "Vue d'ensemble":
        _page_overview(df)
    elif page == "Transactions":
        _page_transactions(df, settings)
    elif page == "Règles":
        _page_rules(settings)
    else:
        # Les projets sont indépendants de la période d'affichage : df complet.
        _page_projects(df_full, settings)


if __name__ == "__main__":
    main()
