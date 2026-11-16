import re
import os


def parse_logs():
    packet_data = []
    # Regex für die UART-Pakete (12 Bytes Payload + 2 Bytes CRC)
    pattern = re.compile(r"d5 55 ([0-9a-f ]{29})([0-9a-f]{2} [0-9a-f]{2})")

    for file in os.listdir('.'):
        if file.endswith(".txt"):
            # FIX: encoding='utf-8' und errors='ignore' überspringt binäre "Fehlermeldungen"
            with open(file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    match = pattern.search(line.lower())
                    if match:
                        payload = [int(b, 16) for b in match.group(1).split()]
                        checksum = [int(b, 16) for b in match.group(2).split()]
                        packet_data.append({'p': payload, 'c': (checksum[0] << 8) | checksum[1]})
    return packet_data


def analyze_deltas(packets):
    print(f"Analysiere {len(packets)} Pakete auf XOR-Deltas...")
    # Wir suchen nach der Beziehung: (CRC_A ^ CRC_B) = f(Payload_A ^ Payload_B)
    delta_map = {}

    for i in range(len(packets)):
        for j in range(i + 1, len(packets)):
            p_xor = bytes([a ^ b for a, b in zip(packets[i]['p'], packets[j]['p'])])
            c_xor = packets[i]['c'] ^ packets[j]['c']

            if p_xor not in delta_map:
                delta_map[p_xor] = set()
            delta_map[p_xor].add(c_xor)

    # Ausgabe der Ergebnisse
    for p_diff, c_diffs in delta_map.items():
        if len(c_diffs) == 1:
            print(f"MUSTER GEFUNDEN: Payload-XOR {p_diff.hex()} -> CRC-XOR {list(c_diffs)[0]:04X}")
        elif len(c_diffs) > 1:
            # Wenn hier mehrere Werte stehen, ist der Algorithmus nicht rein linear/XOR-basiert
            pass


if __name__ == "__main__":
    packets = parse_logs()
    analyze_deltas(packets)