export function feedback(message,error=false){let el=document.querySelector('#feedback');if(el){el.textContent=message;el.className=error?'feedback error':'feedback';}}
let pending=null;
export function flash(message,error=false){pending={message,error}}
export function consumeFlash(){const item=pending;pending=null;return item}
