import { test } from 'node:test';
import assert from 'node:assert/strict';
import { predictionBookingMessage, confirmedPrivateOwner } from '../src/booking_message.ts';
import { DAILY_TITLE, ZONE_LABEL, picksCard } from '../src/format.ts';
const picks=[{sport:'football',league:'England Premier League',home:'Home',away:'Away',label:'Under 3.5 Total Goals',price:1.66,prob:0.6,startUtc:'2030-01-01T14:00:00Z'}];
const booking={code:'UNIT01',selectionCount:1,exactMembershipVerified:true}; // synthetic UI input
test('disabled feature preserves the existing prediction card exactly',()=>{
  assert.deepEqual(predictionBookingMessage(picks,'2030-01-01',booking),{text:picksCard(picks,'2030-01-01',DAILY_TITLE,{zone:ZONE_LABEL}).replace(DAILY_TITLE,`<b>${DAILY_TITLE}</b>`)});
});
test('booking uses existing layout, disclaimer, selectable code and native clipboard JSON',()=>{
  const reply=predictionBookingMessage(picks,'2030-01-01',booking,{enabled:true,ownerTest:true});
  for(const text of ['DAILY PICKS','Combined odds 1.66','3:00 PM','Under 3.5','Bet only what you can afford to lose.','<code>UNIT01</code>','PRIVATE TEST','No wager','Owner confirmation pending']) assert.ok(reply.text.includes(text),text);
  assert.deepEqual(reply.reply_markup,{inline_keyboard:[[{text:'Copy Booking Code',copy_text:{text:'UNIT01'}}]]});
  assert.equal(reply.reply_markup.inline_keyboard[0][0].callback_data,undefined);
});
test('unsupported clipboard client retains monospace fallback without copy button',()=>{
  const reply=predictionBookingMessage(picks,'2030-01-01',booking,{enabled:true,nativeCopy:false});
  assert.ok(reply.text.includes('<code>UNIT01</code>'));assert.equal(reply.reply_markup,undefined);
});
test('failed creation, failed verification, invalid code or partial slip preserve ordinary delivery',()=>{
  const base=predictionBookingMessage(picks,'2030-01-01',null);
  for(const value of [null,{...booking,exactMembershipVerified:false},{...booking,code:'<bad>'},{...booking,selectionCount:2}]) assert.deepEqual(predictionBookingMessage(picks,'2030-01-01',value,{enabled:true}),base);
  assert.equal(predictionBookingMessage(picks,'2030-01-01',{...booking,exactMembershipVerified:false},{enabled:true,ownerTest:true}).text.includes('Verification incomplete'),true);
});
test('owner destination gate rejects groups, other users, ambiguous configuration and missing identity',()=>{
  assert.equal(confirmedPrivateOwner(['123'],{id:123,type:'private'}),'123');
  for(const [admins,chat] of [[['123'],{id:-123,type:'group'}],[['123'],{id:123,type:'supergroup'}],[['123'],{id:456,type:'private'}],[['123','456'],{id:123,type:'private'}],[[],{}],[['-123'],{id:-123,type:'private'}]]) assert.equal(confirmedPrivateOwner(admins,chat),null);
});
