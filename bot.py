from __future__ import annotations

import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "trades.sqlite3"))
TOKEN = os.getenv("DISCORD_TOKEN")


def optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer Discord ID") from exc


STAFF_ROLE_ID = optional_int_env("STAFF_ROLE_ID")
TICKET_CATEGORY_ID = optional_int_env("TICKET_CATEGORY_ID")


class TradeStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                payer_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                amount_ltc TEXT NOT NULL,
                recipient_address TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id INTEGER
            )
            """
        )
        self.connection.commit()

    def create(
        self,
        *,
        guild_id: int,
        channel_id: int,
        payer_id: int,
        seller_id: int,
        amount_ltc: str,
        recipient_address: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO trades (
                guild_id, channel_id, payer_id, seller_id, amount_ltc,
                recipient_address, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_details')
            """,
            (
                guild_id,
                channel_id,
                payer_id,
                seller_id,
                amount_ltc,
                recipient_address,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def get(self, trade_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()

    def get_for_channel(self, channel_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM trades WHERE channel_id = ?", (channel_id,)
        ).fetchone()

    def set_status(self, trade_id: int, status: str) -> None:
        self.connection.execute(
            "UPDATE trades SET status = ? WHERE id = ?", (status, trade_id)
        )
        self.connection.commit()

    def set_message_id(self, trade_id: int, message_id: int) -> None:
        self.connection.execute(
            "UPDATE trades SET message_id = ? WHERE id = ?",
            (message_id, trade_id),
        )
        self.connection.commit()

    def active_trades(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT id FROM trades
                WHERE status != 'closed'
                """
            )
        )


class TradeBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.store = TradeStore(DATABASE_PATH)

        guild_id = optional_int_env("DISCORD_GUILD_ID")
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        for row in self.store.active_trades():
            self.add_view(TradeView(int(row["id"])))


intents = discord.Intents.default()
bot = TradeBot(command_prefix="!", intents=intents)


def trade_embed(trade: sqlite3.Row) -> discord.Embed:
    status_text = {
        "awaiting_details": "Waiting for the seller to confirm the amount and address.",
        "ready_to_pay": "Details confirmed. The payer can send Litecoin directly to the address below.",
        "payment_sent": "The payer marked the payment as sent. This is not blockchain verification.",
        "completed": "The seller confirmed receipt. The bot did not verify or move funds.",
        "disputed": "Disputed. Contact the server's moderators; do not send more funds.",
        "cancelled": "Cancelled before payment was marked sent.",
    }
    embed = discord.Embed(
        title=f"Litecoin trade #{trade['id']}",
        description=status_text[trade["status"]],
        color=discord.Color.gold(),
    )
    embed.add_field(name="Payer", value=f"<@{trade['payer_id']}>")
    embed.add_field(name="Seller", value=f"<@{trade['seller_id']}>")
    embed.add_field(name="Amount", value=f"{trade['amount_ltc']} LTC")
    embed.add_field(
        name="Recipient Litecoin address",
        value=discord.utils.escape_markdown(trade["recipient_address"]),
        inline=False,
    )
    embed.set_footer(
        text="Non-custodial: verify the address with the seller independently. Never share a seed phrase."
    )
    return embed


class TradeView(discord.ui.View):
    def __init__(self, trade_id: int) -> None:
        super().__init__(timeout=None)
        self.trade_id = trade_id
        actions = (
            ("Confirm details", "confirm_details", discord.ButtonStyle.primary),
            ("Mark LTC sent", "mark_sent", discord.ButtonStyle.success),
            ("Confirm receipt", "confirm_receipt", discord.ButtonStyle.success),
            ("Dispute", "dispute", discord.ButtonStyle.danger),
            ("Cancel", "cancel", discord.ButtonStyle.secondary),
            ("Close ticket (staff)", "close_ticket", discord.ButtonStyle.secondary),
        )
        for label, action, style in actions:
            button = discord.ui.Button(
                label=label,
                custom_id=f"trade:{trade_id}:{action}",
                style=style,
            )
            button.callback = self._callback(action)
            self.add_item(button)

    def _callback(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.handle(interaction, action)

        return callback

    async def handle(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        trade = bot.store.get(self.trade_id)
        if trade is None or interaction.guild is None:
            await interaction.response.send_message(
                "This trade could not be found.", ephemeral=True
            )
            return

        actor_id = interaction.user.id
        payer_id = int(trade["payer_id"])
        seller_id = int(trade["seller_id"])
        status = str(trade["status"])

        if action == "confirm_details":
            if actor_id != seller_id or status != "awaiting_details":
                await self.reject(interaction, "Only the seller can confirm pending details.")
                return
            new_status = "ready_to_pay"
        elif action == "mark_sent":
            if actor_id != payer_id or status != "ready_to_pay":
                await self.reject(interaction, "Only the payer can mark a confirmed trade as sent.")
                return
            new_status = "payment_sent"
        elif action == "confirm_receipt":
            if actor_id != seller_id or status != "payment_sent":
                await self.reject(interaction, "Only the seller can confirm receipt after payment is marked sent.")
                return
            new_status = "completed"
        elif action == "dispute":
            if actor_id not in (payer_id, seller_id) or status in (
                "completed",
                "disputed",
                "cancelled",
            ):
                await self.reject(interaction, "This trade cannot be disputed by you.")
                return
            new_status = "disputed"
        elif action == "cancel":
            if (
                actor_id not in (payer_id, seller_id)
                or status not in ("awaiting_details", "ready_to_pay")
            ):
                await self.reject(interaction, "This trade can no longer be cancelled.")
                return
            new_status = "cancelled"
        elif action == "close_ticket":
            if not self.is_staff(interaction.user):
                await self.reject(interaction, "Only server staff can close this ticket.")
                return
            if status not in ("completed", "disputed", "cancelled"):
                await self.reject(
                    interaction, "Resolve, dispute, or cancel the trade before closing it."
                )
                return
            if not isinstance(interaction.channel, discord.TextChannel):
                await self.reject(interaction, "This trade ticket channel cannot be closed.")
                return
            await interaction.response.send_message(
                "Closing this trade ticket.", ephemeral=True
            )
            await interaction.channel.delete(reason=f"Trade #{self.trade_id} closed")
            bot.store.set_status(self.trade_id, "closed")
            return
        else:
            await self.reject(interaction, "Unknown trade action.")
            return

        bot.store.set_status(self.trade_id, new_status)
        updated = bot.store.get(self.trade_id)
        if updated is None:
            await self.reject(interaction, "Trade state could not be loaded.")
            return
        await interaction.response.edit_message(
            embed=trade_embed(updated), view=TradeView(self.trade_id)
        )

    @staticmethod
    def is_staff(user: discord.abc.User) -> bool:
        if not isinstance(user, discord.Member):
            return False
        return user.guild_permissions.manage_channels or (
            STAFF_ROLE_ID is not None
            and any(role.id == STAFF_ROLE_ID for role in user.roles)
        )

    @staticmethod
    async def reject(interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="trade", description="Open a Litecoin trade coordination ticket.")
@app_commands.describe(
    partner="The seller/recipient for this trade",
    amount_ltc="Agreed amount in LTC, up to 8 decimal places",
    recipient_address="Seller's Litecoin receiving address (never a private key)",
)
@app_commands.guild_only()
async def trade(
    interaction: discord.Interaction,
    partner: discord.Member,
    amount_ltc: str,
    recipient_address: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Trades can only be opened in a server.", ephemeral=True
        )
        return
    if partner.bot or partner.id == interaction.user.id:
        await interaction.response.send_message(
            "Choose another human server member as the seller.", ephemeral=True
        )
        return

    try:
        amount = Decimal(amount_ltc)
    except InvalidOperation:
        amount = Decimal("NaN")
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > Decimal("84000000")
        or amount.as_tuple().exponent < -8
    ):
        await interaction.response.send_message(
            "Enter an amount above 0 and no greater than 84,000,000 LTC, with at most 8 decimal places.",
            ephemeral=True,
        )
        return

    address = recipient_address.strip()
    if not address or len(address) > 100 or any(char.isspace() for char in address):
        await interaction.response.send_message(
            "Enter a Litecoin address without spaces.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    if bot_member is None:
        await interaction.followup.send(
            "The bot could not resolve its server member permissions.", ephemeral=True
        )
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        ),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        partner: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    staff_role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    if STAFF_ROLE_ID is not None and staff_role is None:
        await interaction.followup.send(
            "STAFF_ROLE_ID does not identify a role in this server.", ephemeral=True
        )
        return
    if staff_role is not None:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    category = (
        guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    )
    if TICKET_CATEGORY_ID is not None and not isinstance(
        category, discord.CategoryChannel
    ):
        await interaction.followup.send(
            "TICKET_CATEGORY_ID does not identify a category channel.", ephemeral=True
        )
        return

    channel = await guild.create_text_channel(
        name=f"trade-{interaction.user.id}-{partner.id}",
        overwrites=overwrites,
        category=category,
        reason="Litecoin trade ticket",
    )
    try:
        trade_id = bot.store.create(
            guild_id=guild.id,
            channel_id=channel.id,
            payer_id=interaction.user.id,
            seller_id=partner.id,
            amount_ltc=format(amount, "f"),
            recipient_address=address,
        )
        message = await channel.send(
            content=(
                f"{interaction.user.mention} (payer) and {partner.mention} (seller), "
                "review the details. The seller must confirm the amount and address "
                "before the payer sends anything."
            ),
            embed=trade_embed(bot.store.get(trade_id)),
            view=TradeView(trade_id),
        )
        bot.store.set_message_id(trade_id, message.id)
    except (discord.DiscordException, sqlite3.Error):
        await channel.delete(reason="Trade ticket setup failed")
        raise

    await interaction.followup.send(
        f"Trade ticket created: {channel.mention}", ephemeral=True
    )


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Set DISCORD_TOKEN in the environment before starting the bot."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
