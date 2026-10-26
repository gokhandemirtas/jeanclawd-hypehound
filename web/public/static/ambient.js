(function () {
  // Each glyph: char, position, parallax speed, rotation speed, opacity
  var GLYPHS = [
    { char: '{',   top: '12vh', left:  '3vw',              size: '9rem',   speed:  0.08,  rot:  0.018, opacity: 0.040 },
    { char: '}',   top: '46vh',              right: '4vw',  size: '11rem',  speed: -0.12,  rot: -0.025, opacity: 0.032 },
    { char: '+',   top: '28vh', left: '21vw',              size: '5.5rem', speed:  0.17,  rot:  0.050, opacity: 0.055 },
    { char: '//',  top: '66vh', left:  '6vw',              size: '4rem',   speed: -0.09,  rot:  0,     opacity: 0.042 },
    { char: '[',   top: '17vh',              right: '17vw', size: '7.5rem', speed:  0.11,  rot:  0.038, opacity: 0.048 },
    { char: ']',   top: '57vh',              right: '9vw',  size: '9.5rem', speed: -0.15,  rot: -0.022, opacity: 0.030 },
    { char: '#',   top: '80vh', left: '30vw',              size: '6rem',   speed:  0.07,  rot:  0.058, opacity: 0.038 },
    { char: '>_',  top:  '7vh', left: '47vw',              size: '4.5rem', speed: -0.19,  rot:  0,     opacity: 0.052 },
    { char: '/*',  top: '35vh',              right: '27vw', size: '3.5rem', speed:  0.10,  rot:  0.030, opacity: 0.046 },
    { char: '}',   top: '88vh', left: '55vw',              size: '7rem',   speed:  0.14,  rot: -0.040, opacity: 0.033 },
    { char: '+',   top: '52vh', left: '13vw',              size: '3rem',   speed: -0.08,  rot:  0.065, opacity: 0.060 },
    { char: '[ ]', top: '22vh',              right: '38vw', size: '3rem',   speed:  0.21,  rot:  0,     opacity: 0.038 },
  ];

  var container = document.createElement('div');
  container.className = 'ambient-glyphs';
  container.setAttribute('aria-hidden', 'true');

  var shapes = [];

  GLYPHS.forEach(function (g) {
    var el = document.createElement('span');
    el.className = 'ambient-glyph';
    el.textContent = g.char;
    el.style.cssText = [
      'font-size:' + g.size,
      g.top   ? 'top:'   + g.top   : '',
      g.left  ? 'left:'  + g.left  : '',
      g.right ? 'right:' + g.right : '',
    ].filter(Boolean).join(';');
    container.appendChild(el);
    shapes.push({ el: el, speed: g.speed, rot: g.rot });
  });

  // Insert behind all page content
  document.body.insertBefore(container, document.body.firstChild);

  var pending = false;
  function update() {
    var y = window.scrollY;
    document.documentElement.style.setProperty('--background-scroll-y', (y * 0.16) + 'px');
    shapes.forEach(function (s) {
      s.el.style.transform =
        'translate3d(0,' + (y * s.speed) + 'px,0) rotate(' + (y * s.rot) + 'deg)';
    });
    pending = false;
  }

  window.addEventListener('scroll', function () {
    if (!pending) {
      pending = true;
      window.requestAnimationFrame(update);
    }
  }, { passive: true });

  update();
}());
