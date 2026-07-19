(function () {
  const canvas = document.getElementById("loginParticles");
  if (!canvas || !canvas.getContext) {
    return;
  }

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ctx = canvas.getContext("2d");
  const particles = [];
  let width = 0;
  let height = 0;
  let raf = 0;
  let pointer = { x: 0, y: 0, active: false };

  function particleCount() {
    return Math.max(28, Math.min(86, Math.round((width * height) / 18000)));
  }

  function resetParticle(particle) {
    particle.x = Math.random() * width;
    particle.y = Math.random() * height;
    particle.vx = (Math.random() - 0.5) * 0.45;
    particle.vy = (Math.random() - 0.5) * 0.45;
    particle.radius = 1.2 + Math.random() * 2.4;
  }

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    const target = particleCount();
    while (particles.length < target) {
      const particle = {};
      resetParticle(particle);
      particles.push(particle);
    }
    particles.length = target;
  }

  function drawParticle(particle) {
    ctx.beginPath();
    ctx.arc(particle.x, particle.y, particle.radius, 0, Math.PI * 2);
    ctx.fill();
  }

  function updateParticle(particle) {
    particle.x += particle.vx;
    particle.y += particle.vy;

    if (pointer.active) {
      const dx = particle.x - pointer.x;
      const dy = particle.y - pointer.y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance > 0 && distance < 120) {
        const force = (120 - distance) / 120;
        particle.x += (dx / distance) * force * 1.6;
        particle.y += (dy / distance) * force * 1.6;
      }
    }

    if (particle.x < -20 || particle.x > width + 20 || particle.y < -20 || particle.y > height + 20) {
      resetParticle(particle);
    }
  }

  function drawLinks() {
    for (let i = 0; i < particles.length; i += 1) {
      for (let j = i + 1; j < particles.length; j += 1) {
        const a = particles[i];
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        if (distance < 135) {
          ctx.globalAlpha = (1 - distance / 135) * 0.32;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
  }

  function frame() {
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#49afd9";
    ctx.strokeStyle = "#49afd9";
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.54;
    particles.forEach(function (particle) {
      updateParticle(particle);
      drawParticle(particle);
    });
    drawLinks();
    ctx.globalAlpha = 1;
    raf = window.requestAnimationFrame(frame);
  }

  function start() {
    window.cancelAnimationFrame(raf);
    resize();
    if (reducedMotion.matches) {
      frame();
      window.cancelAnimationFrame(raf);
      return;
    }
    raf = window.requestAnimationFrame(frame);
  }

  window.addEventListener("resize", resize);
  window.addEventListener("mousemove", function (event) {
    pointer = { x: event.clientX, y: event.clientY, active: true };
  });
  window.addEventListener("mouseleave", function () {
    pointer.active = false;
  });
  if (reducedMotion.addEventListener) {
    reducedMotion.addEventListener("change", start);
  } else if (reducedMotion.addListener) {
    reducedMotion.addListener(start);
  }
  start();
}());
