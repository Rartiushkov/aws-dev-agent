function revealOnScroll() {
  const targets = document.querySelectorAll("[data-animate], .reveal-card");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) {
        return;
      }
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.16 });

  targets.forEach((target, index) => {
    if (target.classList.contains("reveal-card")) {
      target.style.transitionDelay = `${Math.min(index * 60, 220)}ms`;
    }
    observer.observe(target);
  });
}

function initParticleField() {
  const particleCanvas = document.getElementById("particle-canvas");
  if (!particleCanvas) {
    return;
  }

  const ctx = particleCanvas.getContext("2d");
  if (!ctx) {
    return;
  }

  let width = 0;
  let height = 0;
  let dots = [];
  let rafId = 0;
  let time = 0;
  const gridSize = 34;

  function resize() {
    width = particleCanvas.width = window.innerWidth;
    height = particleCanvas.height = Math.max(window.innerHeight, document.documentElement.scrollHeight);
    const cols = Math.ceil(width / gridSize);
    const rows = Math.ceil(height / gridSize);
    dots = [];

    for (let row = 0; row < rows; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        dots.push({
          x: col * gridSize + 12,
          y: row * gridSize + 12,
          seed: (row * cols + col) * 0.19,
          warm: Math.random() > 0.84,
        });
      }
    }
  }

  function draw() {
    time += 0.012;
    ctx.clearRect(0, 0, width, height);

    for (const dot of dots) {
      const pulse = (Math.sin(time + dot.seed) + 1) / 2;
      const driftX = Math.cos(time * 0.34 + dot.seed) * 0.9;
      const driftY = Math.sin(time * 0.58 + dot.seed) * 1.1;
      const alpha = dot.warm ? 0.04 + pulse * 0.08 : 0.03 + pulse * 0.07;
      const radius = dot.warm ? 0.8 + pulse * 1 : 0.55 + pulse * 0.7;

      ctx.beginPath();
      ctx.fillStyle = dot.warm
        ? `rgba(255, 139, 66, ${alpha})`
        : `rgba(75, 151, 223, ${alpha})`;
      ctx.arc(dot.x + driftX, dot.y + driftY, radius, 0, Math.PI * 2);
      ctx.fill();
    }

    rafId = window.requestAnimationFrame(draw);
  }

  resize();
  draw();

  window.addEventListener("resize", () => {
    window.cancelAnimationFrame(rafId);
    resize();
    draw();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  revealOnScroll();
  initParticleField();
});
