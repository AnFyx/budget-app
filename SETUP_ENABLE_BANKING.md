# Procédure : accéder à tes transactions BNP via Enable Banking

À faire **une seule fois** pour mettre le PoC en service. Compte ~20 minutes.

## Vue d'ensemble

Tu vas créer un compte Enable Banking, enregistrer une « application » (ce qui
génère une paire de clés RSA), puis lancer le consentement sur tes propres
comptes BNP. Tu opères sous l'agrément AISP d'Enable Banking : tu n'as **pas**
besoin de ton propre certificat eIDAS ni d'agrément ACPR.

---

## Étape 1 — Créer un compte sur le Control Panel

1. Va sur `https://enablebanking.com/sign-in/`.
2. Saisis ton email : tu reçois un lien d'authentification à usage unique
   (pas de mot de passe à créer).
3. Clique le lien → tu es dans le Control Panel.

## Étape 2 — Enregistrer une application

C'est l'étape qui génère ta clé privée. **Lis tout avant de cliquer Register.**

1. Ouvre le formulaire d'enregistrement d'application.
2. Renseigne un nom (ex. `budget-perso`).
3. **Environnement** : commence en `SANDBOX` pour tester sans risque, puis
   refais une app en `PRODUCTION` (aussi appelé « live ») pour tes vrais comptes.
4. **Redirect URLs** : ajoute exactement l'URL de callback du PoC :
   ```
   https://127.0.0.1:8765/callback
   ```
   (doit correspondre au pied de la lettre à `redirect_url` du PoC). HTTPS est
   obligatoire, même en local : au retour de la banque, le navigateur affichera
   un avertissement de certificat auto-signé, à accepter pour finaliser.
5. **Clé** : choisis l'option de génération de clé. Deux possibilités :
   - *Génération navigateur (SubtleCrypto)* : le navigateur crée la paire et te
     fait télécharger la clé privée. **Recommandé** : la clé privée ne transite
     jamais par le réseau.
   - Ou téléverse toi-même un certificat public que tu as généré localement.
6. Clique **Register**. Ton navigateur télécharge un fichier `.pem` nommé
   d'après l'ID de l'application (ex. `aaaaaaaa-bbbb-...-eeeeeeeeeeee.pem`).
   **C'est ta clé privée. Tu ne pourras pas la re-télécharger.**

## Étape 3 — Sécuriser la clé privée (important)

La clé `.pem` est le secret le plus sensible : quiconque l'a peut s'authentifier
comme ton application.

1. Déplace-la hors de tout dossier versionné, par ex. :
   ```bash
   mkdir -p ~/.secrets && mv ~/Downloads/aaaaaaaa-*.pem ~/.secrets/enablebanking_app.pem
   chmod 600 ~/.secrets/enablebanking_app.pem
   ```
2. **Recommandé** : chiffre-la par passphrase. Si elle a été générée en clair :
   ```bash
   openssl pkcs8 -topk8 -in ~/.secrets/enablebanking_app.pem \
       -out ~/.secrets/enablebanking_app.enc.pem
   # (demande une passphrase ; remplace ensuite l'originale par la version chiffrée)
   ```
   Reporte cette passphrase dans `EB_PRIVATE_KEY_PASSPHRASE` (.env).

## Étape 4 — Renseigner le `.env` du PoC

```bash
cp .env.example .env
```
Remplis :
- `EB_APPLICATION_ID` = l'ID de l'application (= le nom du fichier .pem, sans `.pem`).
- `EB_PRIVATE_KEY_PATH` = `~/.secrets/enablebanking_app.pem`.
- `EB_PRIVATE_KEY_PASSPHRASE` = la passphrase de l'étape 3 (si chiffrée).
- `BUDGET_MASTER_PASSPHRASE` = une passphrase forte, différente, pour le coffre local.
- `EB_ASPSP_NAME` : à confirmer à l'étape suivante.

Charge le `.env` dans ton shell (ou utilise un outil type `direnv`) :
```bash
set -a && source .env && set +a
```

## Étape 5 — Trouver le nom exact de BNP

```bash
export PYTHONPATH=.
python -m budget_poc.cli list-banks
```
Repère l'entrée BNP (il peut y avoir plusieurs marques : BNP Paribas,
Hello bank!, BNP Paribas Entreprise). Copie le **nom exact** affiché dans
`EB_ASPSP_NAME`, et vérifie que `personal` figure bien dans ses `psu_types`.

## Étape 6 — Consentement et première récupération

```bash
python -m budget_poc.cli connect
```
- Le navigateur ouvre la page BNP. **Vérifie que c'est bien Enable Banking qui
  demande l'accès** avant de valider (réflexe anti-phishing).
- Fais ta SCA (app Ma Banque / 2FA). N'ouvre pas ce lien dans une WebView
  embarquée : un vrai navigateur (Safari/Chrome/Firefox), sinon la bascule vers
  l'app SCA peut échouer.
- Au retour, le PoC capte le code, crée la session, la chiffre localement.

```bash
python -m budget_poc.cli fetch --from 2026-01-01
```

## En cas de problème

- **401 sur les appels** : revérifie `iss`/`aud` du JWT dans la doc Enable
  Banking (section « jwtAuthentication » de l'API reference), et que la clé
  privée correspond bien à l'application enregistrée.
- **Banque absente de `list-banks`** : vérifie le pays (`EB_ASPSP_COUNTRY=FR`)
  et que tu es bien sur l'environnement PRODUCTION, pas SANDBOX.
- **Le consentement expire (~90 j)** : relance simplement `connect`.

## Révoquer à tout moment

Deux niveaux, indépendants :
- Côté PoC : `python -m budget_poc.cli revoke`.
- Côté BNP : dans ton espace client BNP, section gestion des accès / prestataires
  DSP2, tu peux couper l'accès d'Enable Banking sans même toucher au PoC.
