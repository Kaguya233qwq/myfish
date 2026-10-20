export default async function teardown() {
  await fetch("http://127.0.0.1:18787/__test__/shutdown", { method: "POST" });
}
