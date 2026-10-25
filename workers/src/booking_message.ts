import { DAILY_TITLE, ZONE_LABEL, picksCard, type PickLineInput } from './format.ts';

export interface BookingPresentation {
  code: string;
  selectionCount: number;
  exactMembershipVerified: boolean;
}

export function bookingCopyKeyboard(code: string) {
  return { inline_keyboard: [[{ text: 'Copy Booking Code', copy_text: { text: code } }]] };
}

/** Pure presentation only: no generation, network, recipient lookup or delivery. */
export function predictionBookingMessage(
  picks: PickLineInput[], day: string, booking: BookingPresentation | null,
  options: { enabled?: boolean; ownerTest?: boolean; nativeCopy?: boolean } = {},
) {
  const card = picksCard(picks, day, DAILY_TITLE, { zone: ZONE_LABEL });
  const base = { text: card.replace(DAILY_TITLE, `<b>${DAILY_TITLE}</b>`) };
  if (!options.enabled || !booking || !/^[A-Z0-9]{4,16}$/.test(booking.code)
      || !picks.length || booking.selectionCount !== picks.length
      || (!booking.exactMembershipVerified && !options.ownerTest)) return base;
  const notice = options.ownerTest ? '<b>KOVQI · PRIVATE TEST</b>\nNo wager has been placed.\n\n' : '';
  const status = options.ownerTest
    ? (booking.exactMembershipVerified ? 'Readback checked. Owner confirmation pending.' : 'Verification incomplete. Owner confirmation required.')
    : 'Slip verified.';
  const text = `${notice}${base.text}\n\n<b>SportyBet Nigeria booking code</b>\n<code>${booking.code}</code>\nCopy the code and load it in SportyBet Nigeria.\n${status}`;
  return { text, ...(options.nativeCopy === false ? {} : {
    reply_markup: bookingCopyKeyboard(booking.code),
  }) };
}

/** Owner test gate deliberately has no default/group destination or subscriber path. */
export function confirmedPrivateOwner(adminIds: string[], chat: { id?: unknown; type?: unknown }): string | null {
  if (adminIds.length !== 1 || !/^[1-9]\d*$/.test(adminIds[0])) return null;
  return chat.type === 'private' && String(chat.id) === adminIds[0] ? adminIds[0] : null;
}
