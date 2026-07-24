function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);
let clientX = getRandomInt(100, 800);
let clientY = getRandomInt(100, 600);
let pageX = screenX + getRandomInt(0, 200);
let pageY = screenY + getRandomInt(0, 200);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
Object.defineProperty(MouseEvent.prototype, 'clientX', { value: clientX });
Object.defineProperty(MouseEvent.prototype, 'clientY', { value: clientY });
Object.defineProperty(MouseEvent.prototype, 'pageX', { value: pageX });
Object.defineProperty(MouseEvent.prototype, 'pageY', { value: pageY });
Object.defineProperty(MouseEvent.prototype, 'offsetX', { value: getRandomInt(10, 200) });
Object.defineProperty(MouseEvent.prototype, 'offsetY', { value: getRandomInt(10, 200) });

// Patch PointerEvent properties too
if (typeof PointerEvent !== 'undefined') {
    Object.defineProperty(PointerEvent.prototype, 'screenX', { value: screenX });
    Object.defineProperty(PointerEvent.prototype, 'screenY', { value: screenY });
    Object.defineProperty(PointerEvent.prototype, 'clientX', { value: clientX });
    Object.defineProperty(PointerEvent.prototype, 'clientY', { value: clientY });
    Object.defineProperty(PointerEvent.prototype, 'pageX', { value: pageX });
    Object.defineProperty(PointerEvent.prototype, 'pageY', { value: pageY });
}

// Patch navigator.webdriver to avoid detection
Object.defineProperty(navigator, 'webdriver', { value: false });
