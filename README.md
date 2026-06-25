# PoC budget BNP — agrégation via Enable Banking (AIS, lecture seule)

Récupère tes transactions et soldes BNP via l'API Enable Banking (open banking
DSP2), en **lecture seule**, avec stockage **chiffré au repos**. Pensé comme
socle d'une app de budget : le connecteur bancaire est isolé du futur moteur de
catégorisation, pour pouvoir changer d'agrégateur sans tout réécrire.

## Périmètre et garanties

- **Lecture seule.** Aucun endpoint de paiement (PIS) n'est exposé : le scope du
  consentement est restreint à `balances` + `transactions`. Une erreur de code
  ne peut pas initier de virement.
- **Données chiffrées au repos.** Session, comptes et tout état local sont
  chiffrés en AES-256-GCM, clé dérivée d'une passphrase maître via scrypt.
  Rien de bancaire n'est écrit en clair sur le disque.
- **Secrets hors dépôt.** `.env`, clés `.pem` et stockage `.enc` sont dans
  `.gitignore`. Permissions fichier restreintes au propriétaire (0600).
- **Seul Enable Banking voit les données.** Aucun autre tiers : un unique flux
  HTTPS (certificat vérifié) vers `api.enablebanking.com`, et un serveur de
  callback lié à `127.0.0.1` uniquement.

## Architecture

```
budget_poc/
├── config.py           Config fail-closed depuis l'environnement
├── secure_store.py     Coffre chiffré AES-256-GCM (scrypt) : secrets + session
├── store.py            Persistance SQLite des transactions (synchro idempotente)
├── tls.py              Certificat auto-signé pour le callback HTTPS local
├── eb_client.py        Client API Enable Banking (AIS, lecture seule)
├── callback_server.py  Serveur loopback HTTPS : capte le code OAuth
└── cli.py              Orchestration : list-banks / connect / fetch / show / revoke
```

Découplage volontaire : `eb_client` ne connaît rien du budget, `store` ne connaît
rien du réseau, `cli` orchestre. Le futur moteur de catégorisation consommera les
transactions normalisées de `store` sans toucher à la couche bancaire.

## Deux stockages séparés, deux niveaux de protection

- **Coffre chiffré** (`store.enc`, AES-256-GCM) : secrets, session, IBAN complet.
- **Base SQLite** (`transactions.db`, en clair) : les transactions. Choix assumé
  — données non secrètes, confidentialité au repos déléguée au chiffrement disque
  (BitLocker). L'IBAN n'y figure que tronqué (4 derniers caractères).

## Usage

Procédure d'enregistrement Enable Banking détaillée dans
`SETUP_ENABLE_BANKING.md` (à faire **une fois** avant tout).

```bash
export PYTHONPATH=.

# 1. Trouver le nom exact de BNP, à reporter dans EB_ASPSP_NAME
python -m budget_poc.cli list-banks

# 2. Donner son consentement (ouvre le navigateur → SCA BNP)
python -m budget_poc.cli connect

# 3. Rafraîchir : rapatrie les transactions en base (le « bouton refresh »).
#    SEUL point qui appelle l'API → à déclencher manuellement, pas en boucle.
python -m budget_poc.cli fetch --from 2026-01-01

# 4. Consulter (lecture base locale, AUCUN appel réseau → pas de rate limit)
python -m budget_poc.cli show --from 2026-01-01      # avec catégorie
python -m budget_poc.cli summary --from 2026-01-01   # totaux par catégorie
python -m budget_poc.cli unknown                     # libellés non catégorisés
python -m budget_poc.cli show --raw 3                # JSON brut, pour inspecter

# 5. Révoquer l'accès et purger l'état chiffré (la base n'est pas supprimée)
python -m budget_poc.cli revoke
```

## Catégorisation par règles

Les transactions sont catégorisées à la **lecture** (jamais stockées avec une
catégorie figée) : modifier une règle re-catégorise tout l'historique. Les règles
vivent dans `rules.json` (dans `data_dir`), éditable à la main ou par le futur
front. Le fichier est créé au premier usage à partir des défauts
(voir `rules.default.json` à la racine pour référence).

Une règle = `{category, type, keywords}` ; `type` vaut `any`/`debit`/`credit`
pour filtrer sur le sens (distinguer un virement reçu d'un émis). Les règles sont
une **liste ordonnée** : la première qui matche gagne, donc on place le spécifique
avant le général. Une transaction qu'aucune règle ne couvre tombe dans `Inconnue` ;
`unknown` liste ces libellés pour que tu ajoutes les règles manquantes.

## Synchro idempotente (déduplication)

`fetch` re-récupère une fenêtre qui recouvre l'existant et fusionne par upsert sur
une clé d'identité stable (`entry_reference` de la banque, ou hash déterministe à
défaut). Conséquence : rejouer `fetch` ne crée jamais de doublon, et le passage
*pending → booked* (où date et statut changent) met à jour la transaction en place
au lieu de la dupliquer.

## Front (app locale Streamlit)

Interface graphique locale, réutilisant directement les modules back (pas d'API
HTTP). **Lance-la toujours via `run_app.py`**, qui force l'écoute sur la loopback
(`127.0.0.1`) — Streamlit, par défaut, s'exposerait sur tout le réseau local.

```bash
python run_app.py
```

Trois pages (barre latérale) :
- **Vue d'ensemble** : camembert des dépenses par catégorie ; clic (ou sélecteur)
  sur une part → détail des transactions. Plus dépenses/recettes/solde net.
- **Transactions** : liste filtrable (catégorie, texte). Mini-formulaire pour
  transformer un libellé en règle (mot-clé pré-rempli depuis le marchand, éditable).
- **Règles** : éditeur tabulaire de `rules.json` (ajout/modif/suppression).

Sécurité de l'app :
- L'affichage ne lit que la base et `rules.json` (en clair) : aucune passphrase.
- La **passphrase maître n'est demandée qu'au clic sur « Rafraîchir »**, utilisée
  le temps de l'appel, puis effacée de l'environnement. Jamais conservée dans
  l'état de session Streamlit.
- Le bouton « Rafraîchir » est grisé si le consentement est expiré (l'app lit
  l'expiration en clair, sans toucher au coffre) → relancer `connect` en CLI.
- Créer une règle l'insère **en tête** (priorité max) : la dernière créée l'emporte.

## Limites assumées (PoC)

- **Catégorisation hors périmètre.** C'est là que se trouve 90 % de la valeur
  d'une vraie app ; le PoC s'arrête à la récupération propre des données.
- **Renouvellement du consentement.** Le consentement DSP2 expire (≈ 90 jours) ;
  il faut relancer `connect`. Pas de refresh automatique ici.
- **Dépendance Enable Banking.** Leur tier « Restricted Production » gratuit peut
  changer (cf. GoCardless qui a fermé le sien). Le découplage limite l'impact.
- **Callback en HTTP loopback.** Suffisant en local ; une vraie app web exigera
  un `redirect_url` HTTPS public enregistré côté Enable Banking.


powershell -ExecutionPolicy Bypass -File .\start.ps1
