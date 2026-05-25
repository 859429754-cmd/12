/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"Cascadia Code"', '"JetBrains Mono"', "Consolas", "monospace"],
        sans: ['Inter', '"Microsoft YaHei"', "system-ui", "sans-serif"]
      }
    }
  },
  plugins: []
};
