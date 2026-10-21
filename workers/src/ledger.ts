/**
 * Issued-pick ledger for Custom Picks and Pro Picks (migrations/0014).
 *
 * Top/Daily already have a complete issued-selection + settlement history
 * via picks/prediction_memberships + predbot's existing settlement grader
 * (predbot/markets/settlement.py). Custom Picks and Pro Picks never had an
 * equivalent — a ticket is formatted and sent, then forgotten. This module
 * is the missing record, built and tested, but NOT yet wired into the live
 * send paths (enterProPicks/postProPicksToAdmin/processCustomPicksText) and
 * NOT applied to production D1 — see migration 0014's own header. Wiring
 * either of those in before the migration is live would crash the send
 * path outright (INSERT against a table that doesn't exist yet), so that
 * integration is the deliberate next, approval-gated step, not done here.
 *
 * This module never grades anything itself — "avoid creating a second
 * contradictory grader" (the brief's own words). `settleIssuedLeg()` only
 * RECORDS an outcome some existing grader already determined (predbot's
 * settlement.py, the same vocabulary Top/Daily already use: won | lost |
 * push | half_won | half_lost | void | pending), idempotently.
 */
import type { Candidate } from "./custom_picks.ts";
import type { ProTicket } from "./pro_picks.ts";

export type SettlementStatus = "pending" | "won" | "lost" | "push" | "half_won" | "half_lost" | "void";

/** Small, fast, non-cryptographic hash (FNV-1a, 32-bit) — this id only
 * needs to be a stable, collision-unlikely fingerprint of a ticket's actual
 * content, never a security boundary, so a full crypto digest (and the
 * async API that would force on every caller) isn't warranted. */
function fnv1a(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

/** Deterministic fingerprint of WHAT was actually sent — one leg per
 * fixture/market/selection/line, sorted so leg order never matters. Two
 * tickets with the same legs hash identically; two tickets with different
 * legs (even for the same user on the same day) hash differently. This is
 * the actual idempotency mechanism: it lets "the same ticket, resent" and
 * "a genuinely different ticket, same day" be told apart, which a plain
 * (feature, user, chat, day) key cannot do (see migration 0014's comment —
 * Custom Picks is conversational and can issue several distinct tickets a
 * day). */
export function legsKey(picks: Candidate[]): string {
  const parts = picks
    .map((p) => `${p.fixtureId}|${p.market}|${p.selection}|${p.line ?? ""}|${p.period}|${p.stat}|${p.entity}`)
    .sort();
  return fnv1a(parts.join("~"));
}

/**
 * Deterministic content-derived id — NOT a random uuid — so issuing the
 * "same" ticket twice (a retried webhook, a cron firing twice before the
 * first send's receipt lands) can never create two rows, while a
 * genuinely different ticket for the same user/day gets its own id. The
 * unique index on (feature, user_id, chat_id, prediction_date, legs_key)
 * is the enforcement layer; this is just what makes that enforcement
 * meaningful rather than accidental.
 */
export function ticketId(feature: "custom" | "pro", userId: string, chatId: string, predictionDate: string, picks: Candidate[]): string {
  return `${feature}:${userId}:${chatId}:${predictionDate}:${legsKey(picks)}`;
}

/**
 * Records a ticket that was ACTUALLY sent (call this only after a
 * successful Telegram send, never before, and never for a dry-run/preview
 * — a recorded-but-never-sent ticket would be a ledger that lies).
 * Idempotent against RETRIES of the exact same ticket (same feature, user,
 * chat, day, AND legs) — a silent no-op, INSERT OR IGNORE, legs only
 * written when the ticket row itself was newly inserted. A genuinely
 * different ticket (different legs) for the same user/day is NOT
 * deduplicated — it gets its own row, by design (see legsKey's doc
 * comment): Custom Picks can issue several distinct tickets in one day.
 */
export async function recordIssuedTicket(
  db: D1Database,
  feature: "custom" | "pro",
  userId: string,
  chatId: string,
  predictionDate: string,
  timezone: string,
  publishedAtIso: string,
  ticket: ProTicket | { picks: Candidate[]; odds: number },
): Promise<{ id: string; inserted: boolean }> {
  const id = ticketId(feature, userId, chatId, predictionDate, ticket.picks);
  const result = await db
    .prepare(
      `INSERT INTO issued_tickets (id, feature, user_id, chat_id, prediction_date, timezone, legs_key, published_at, combined_odds, leg_count, status, created_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'issued', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
       ON CONFLICT(feature, user_id, chat_id, prediction_date, legs_key) DO NOTHING`,
    )
    .bind(id, feature, userId, chatId, predictionDate, timezone, legsKey(ticket.picks), publishedAtIso, ticket.odds, ticket.picks.length)
    .run();
  const inserted = (result.meta?.changes ?? 0) > 0;
  if (inserted) {
    for (let i = 0; i < ticket.picks.length; i++) {
      const p = ticket.picks[i];
      await db
        .prepare(
          `INSERT INTO issued_ticket_legs (ticket_id, leg_index, canonical_event_id, sport, home, away, market, period, stat, entity, selection, line, price, probability, probability_basis, bookmaker_count, settlement_status)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, 'p_model', ?15, 'pending')`,
        )
        .bind(
          id, i, p.fixtureId, p.sport, p.home, p.away, p.market, p.period, p.stat, p.entity, p.selection,
          p.line, p.price, p.probability, Math.round(p.books),
        )
        .run();
    }
  }
  return { id, inserted };
}

/** Unit-stake return multiplier for a settled leg — standard sportsbook
 * definitions, matching predbot/model/payoff.py's Payoff.win_stake/
 * loss_stake averaging for half outcomes (half_won: half the stake wins
 * at price, half is refunded; half_lost: half is lost, half refunded). */
export function legReturn(status: SettlementStatus, price: number): number | null {
  switch (status) {
    case "won": return price;
    case "lost": return 0;
    case "push": case "void": return 1;
    case "half_won": return (1 + price) / 2;
    case "half_lost": return 0.5;
    case "pending": return null;
  }
}

/**
 * Records a leg's outcome — idempotent: once a leg is no longer 'pending'
 * this is a no-op (WHERE settlement_status = 'pending'), so a retried or
 * duplicate settlement sweep can never flip an already-settled leg's
 * recorded outcome. Marks the parent ticket 'settled' once every one of
 * its legs has a non-pending status.
 */
export async function settleIssuedLeg(
  db: D1Database,
  ticketIdValue: string,
  legIndex: number,
  status: Exclude<SettlementStatus, "pending">,
  price: number,
): Promise<boolean> {
  const multiplier = legReturn(status, price);
  const result = await db
    .prepare(
      `UPDATE issued_ticket_legs SET settlement_status = ?3, payout_multiplier = ?4, settled_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
       WHERE ticket_id = ?1 AND leg_index = ?2 AND settlement_status = 'pending'`,
    )
    .bind(ticketIdValue, legIndex, status, multiplier)
    .run();
  const settled = (result.meta?.changes ?? 0) > 0;
  if (settled) {
    const remaining = await db
      .prepare(`SELECT COUNT(*) AS n FROM issued_ticket_legs WHERE ticket_id = ?1 AND settlement_status = 'pending'`)
      .bind(ticketIdValue)
      .first<{ n: number }>();
    if ((remaining?.n ?? 1) === 0) {
      await db.prepare(`UPDATE issued_tickets SET status = 'settled' WHERE id = ?1`).bind(ticketIdValue).run();
    }
  }
  return settled;
}
