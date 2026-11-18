# Litecoin Discord Trade Coordinator

A small Discord bot for coordinating a trade between two members. It creates a
private ticket with buttons to confirm trade details, mark a payment as sent,
confirm receipt, dispute, cancel, and close the ticket.

## Important limitations

- This is **not escrow**. Litecoin is sent directly to the seller's address.
- The bot never asks for or stores a wallet seed phrase or private key, and
  cannot hold, release, or reverse funds.
- Payment buttons record what users declare; the bot does not check the
  Litecoin blockchain. Verify payments independently before treating a trade as
  paid.
- Confirm the recipient address with the seller through a separate trusted
  channel before sending LTC. A Discord ticket is not proof of identity.

## Setup

1. Create a Discord application and bot, invite it with the `bot` and
   `applications.commands` scopes, and grant permissions to manage channels,
   send messages, and read message history.
2. Copy `.env.example` values into your environment. Set `DISCORD_TOKEN`.
   Optionally set a guild ID, staff role ID, and ticket category ID.
3. Install dependencies and start the bot:

   ```sh
   python -m pip install -r requirements.txt
   python bot.py
   ```

4. In a server, run `/trade` with the seller, agreed LTC amount, and seller's
   receiving address. The seller confirms the details before the payer can mark
   the payment as sent.

The bot creates `trades.sqlite3` in its working directory by default. Keep the
database backed up if you need trade-state history.
