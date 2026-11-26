# Storie YouTube + Amazon

Bot Telegram. Gli mandi:

- un link YouTube: risponde con due PNG, la storia 1080x1920 (card con copertina,
  durata, avatar, titolo e canale su sfondo coi colori della copertina) e la sola
  card con gli angoli trasparenti;
- un link Amazon: risponde con il link affiliato pulito (`/dp/ASIN?tag=...`).

- `/storia` e un link Amazon: risponde con la storia 1080x1920 dell'offerta (etichetta in alto,
  card con foto, nome, prezzo scontato e prezzo pieno barrato) e il link affiliato. Sotto il link
  si possono cambiare i campi, uno per riga: `nome:`, `prezzo:`, `prima:`, `etichetta:`, `nota:`.
  Il font è Gotham, che non sta nel repo: il bot lo prende da `~/Library/Fonts` o da `AMZ_PROXY`,
  altrimenti usa Inter.

## Configurazione

`.env` (vedi `.env.example`):

- `TELEGRAM_BOT_TOKEN`: token da @BotFather
- `TELEGRAM_CHAT_ID`: l'unica chat a cui risponde (`python3 bot.py --chat-id`)
- `AFFILIATE_TAG`: tag affiliato Amazon
- `AMZ_PROXY`, `AMZ_PROXY_KEY` (facoltativi): proxy per le pagine Amazon. Da GitHub Actions
  Amazon non dà le pagine e i link corti portano ad amazon.com, quindi lì servono.
  Senza, il bot legge Amazon direttamente (va bene in locale).

Su GitHub Actions gli stessi valori vanno nei secrets: `gh secret set -f .env`.

## Prova locale

    pip install pillow
    python3 bot.py --test https://youtu.be/<id>   # scrive storia.png e copertina.png
    python3 bot.py                                # avvia il bot

Font: Inter (SIL Open Font License, vedi fonts/LICENSE.txt).
