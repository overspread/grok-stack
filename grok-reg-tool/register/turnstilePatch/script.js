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

if (typeof PointerEvent !== 'undefined') {
    Object.defineProperty(PointerEvent.prototype, 'screenX', { value: screenX });
    Object.defineProperty(PointerEvent.prototype, 'screenY', { value: screenY });
    Object.defineProperty(PointerEvent.prototype, 'clientX', { value: clientX });
    Object.defineProperty(PointerEvent.prototype, 'clientY', { value: clientY });
    Object.defineProperty(PointerEvent.prototype, 'pageX', { value: pageX });
    Object.defineProperty(PointerEvent.prototype, 'pageY', { value: pageY });
}

Object.defineProperty(navigator, 'webdriver', { value: false });

// Patch navigator properties to avoid fingerprinting
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' })),
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => getRandomInt(4, 8) });
Object.defineProperty(navigator, 'deviceMemory', { get: () => getRandomInt(4, 8) });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// Override chrome.runtime to avoid detection
try {
    if (window.chrome && chrome.runtime) {
        Object.defineProperty(chrome.runtime, 'connect', { value: () => ({}) });
    }
} catch(e) {}

// Hide HeadlessChrome from user agent
try {
    const uaDesc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'userAgent') || {};
    if (!uaDesc.get) {
        Object.defineProperty(Navigator.prototype, 'userAgent', {
            get: () => 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
        });
    }
} catch(e) {}

// Spoof WebGL vendor/renderer
try {
    const getExt = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function() {
        const ctx = getExt.apply(this, arguments);
        if (ctx && ctx.getParameter) {
            const origGetParameter = ctx.getParameter.bind(ctx);
            ctx.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.';
                if (p === 37446) return 'Intel Iris OpenGL Engine';
                return origGetParameter(p);
            };
        }
        return ctx;
    };
} catch(e) {}
