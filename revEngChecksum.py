import re
import os


def parse_logs_robust():
    packet_data = []
    # Sucht 'd5 55' gefolgt von einer beliebigen Anzahl an Hex-Paaren (Leerzeichen erlaubt)
    # Wir suchen insgesamt 12 Paare (2 Header + 10 Payload + 2 Checksum)
    # Wenn wir d5 55 haben, brauchen wir noch 12 weitere Paare
    pattern = re.compile(r"d5\s*55((?:\s*[0-9a-f]{2}){12})", re.IGNORECASE)

    for file in os.listdir('.'):
        if file.endswith(".txt"):
            with open(file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().lower()
                # Findet alle Vorkommen in der ganzen Datei (auch bei Zeilenumbrüchen)
                matches = pattern.finditer(content)
                for match in matches:
                    # Zerlege die gefundene Kette in Bytes
                    hex_str = match.group(1).replace(" ", "")
                    # Wir haben 12 Paare (24 Zeichen)
                    bytes_val = [int(hex_str[i:i + 2], 16) for i in range(0, 24, 2)]
                    # payload: 10 bytes, checksum: 2 bytes
                    payload = bytes_val[:10]
                    checksum = (bytes_val[10] << 8) | bytes_val[11]
                    packet_data.append({'p': payload, 'c': checksum, 'raw': bytes_val})
    return packet_data


def analyze_deltas(packets):
    print(f"Gefundene Pakete: {len(packets)}")
    delta_map = {}

    for i in range(len(packets)):
        for j in range(i + 1, len(packets)):
            if packets[i]['p'] == packets[j]['p']: continue

            p_xor = bytes([a ^ b for a, b in zip(packets[i]['p'], packets[j]['p'])])
            c_xor = packets[i]['c'] ^ packets[j]['c']

            if p_xor not in delta_map: delta_map[p_xor] = set()
            delta_map[p_xor].add(c_xor)

    for p_diff, c_diffs in delta_map.items():
        if len(c_diffs) == 1:
            print(f"MUSTER: Payload-Delta {p_diff.hex()} -> CRC-Delta {list(c_diffs)[0]:04X}")


if __name__ == "__main__":
    packets = parse_logs_robust()
    if packets:
        analyze_deltas(packets)
    else:
        print("Nichts gefunden. Prüfe ob 'd5 55' in den Logs vorkommt.")